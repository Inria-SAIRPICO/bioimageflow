from __future__ import annotations

import json
import multiprocessing
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path, PurePosixPath
from typing import Any

import pytest

import bioimageflow.launcher.psij as psij_module
from bioimageflow.launcher import (
    LauncherProtocolError,
    PSIJSubmissionUncertainError,
)
from bioimageflow.launcher.psij import _build_spec, launch_psij, observe_psij
from bioimageflow.launcher.psij_artifacts import install_intent
from bioimageflow.launcher.repository import LauncherRepository
from bioimageflow.launcher.types import PSIJLaunchConfig
from tests.unit.launcher.test_psij import (
    _Executor,
    _Job,
    _JobExecutor,
    _JobState,
    _ORIGINAL_LOAD_RUNTIME,
    _control,
    _launch_config,
    _runtime,
)


@pytest.fixture(autouse=True)
def _fake_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    _Executor.states = {}
    _Executor.specs = []
    _Executor.submit_count = 0
    _Executor.instances = []
    _JobExecutor.names = {"slurm", "pbs", "lsf"}
    monkeypatch.setattr(psij_module, "_load_runtime", _runtime)


def _install_intent_in_process(
    storage_root: str,
    run_id: str,
    work_dir: str,
    ready: Any,
    start: Any,
    results: Any,
) -> None:
    control = LauncherRepository(storage_root).open(run_id)
    launch = PSIJLaunchConfig.from_dict(control.read_submission()["launch"])
    ready.put(True)
    if not start.wait(timeout=5):
        raise RuntimeError("intent race did not start")
    _intent, created = install_intent(control, launch, Path(work_dir))
    results.put(created)


def test_psij_job_spec_matches_the_pinned_runtime_api(tmp_path: Path) -> None:
    control = _control(tmp_path)
    launch_psij(control, _launch_config())
    intent = json.loads(
        (control.control_dir / "psij_intent.json").read_text()
    )
    runtime = _ORIGINAL_LOAD_RUNTIME()

    spec = _build_spec(runtime, intent["job"])
    config = runtime.JobExecutorConfig(
        work_directory=control.control_dir / "psij/executor"
    )

    assert isinstance(spec, runtime.JobSpec)
    assert isinstance(spec.resources, runtime.ResourceSpecV1)
    assert isinstance(spec.attributes, runtime.JobAttributes)
    assert spec.resources.node_count == 1
    assert spec.attributes.duration == timedelta(minutes=45)
    assert config.work_directory == control.control_dir / "psij/executor"


def test_attach_timeout_normalizes_new_to_unknown(tmp_path: Path) -> None:
    control = _control(tmp_path)
    launch_psij(control, _launch_config())
    _Executor.states["job-1"] = _JobState.NEW

    observation = observe_psij(control)

    assert observation.state is None
    assert control.read_progress()[-1]["payload"] == {
        "schema": "bioimageflow.launcher.backend_event.v1",
        "event": "psij_unknown",
        "executor": "slurm",
        "native_id": "job-1",
        "state": None,
        "message": None,
    }


def test_missing_or_unsafe_job_work_directory_fails_before_intent(
    tmp_path: Path,
) -> None:
    missing = tmp_path / "missing"
    missing_launch = _launch_config(
        work_dir=PurePosixPath(str(missing))
    )
    missing_control = _control(tmp_path / "missing-case", launch=missing_launch)

    with pytest.raises(LauncherProtocolError, match="unavailable"):
        launch_psij(missing_control, missing_launch)

    real = tmp_path / "real"
    real.mkdir()
    symlink = tmp_path / "linked"
    symlink.symlink_to(real, target_is_directory=True)
    symlink_launch = _launch_config(
        work_dir=PurePosixPath(str(symlink))
    )
    symlink_control = _control(tmp_path / "symlink-case", launch=symlink_launch)

    with pytest.raises(LauncherProtocolError, match="non-symlink"):
        launch_psij(symlink_control, symlink_launch)

    assert _Executor.submit_count == 0
    assert not (missing_control.control_dir / "psij_intent.json").exists()
    assert not (symlink_control.control_dir / "psij_intent.json").exists()


def test_launch_rejects_config_different_from_allocated_submission(
    tmp_path: Path,
) -> None:
    control = _control(tmp_path, launch=_launch_config(executor="slurm"))

    with pytest.raises(LauncherProtocolError, match="allocated run"):
        launch_psij(control, _launch_config(executor="pbs"))

    assert _Executor.instances == []
    assert not (control.control_dir / "psij_intent.json").exists()


def test_two_launchers_racing_across_intent_submit_exactly_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    control = _control(tmp_path)
    submit_entered = threading.Event()
    release_submit = threading.Event()
    original_submit = _Executor.submit

    def blocking_submit(self: _Executor, job: _Job) -> None:
        submit_entered.set()
        assert release_submit.wait(timeout=5)
        original_submit(self, job)

    monkeypatch.setattr(_Executor, "submit", blocking_submit)
    with ThreadPoolExecutor(max_workers=2) as pool:
        winner = pool.submit(launch_psij, control, _launch_config())
        assert submit_entered.wait(timeout=5)
        loser = pool.submit(launch_psij, control, _launch_config())
        with pytest.raises(PSIJSubmissionUncertainError):
            loser.result(timeout=5)
        release_submit.set()
        result = winner.result(timeout=5)

    assert result.native_id == "job-1"
    assert _Executor.submit_count == 1
    assert (control.control_dir / "psij_job.json").is_file()


def test_two_processes_elect_exactly_one_submit_intent_owner(
    tmp_path: Path,
) -> None:
    control = _control(tmp_path)
    work_dir = control.confined_path("psij/executor")
    work_dir.mkdir(parents=True)
    context = multiprocessing.get_context("spawn")
    ready = context.Queue()
    results = context.Queue()
    start = context.Event()
    processes = [
        context.Process(
            target=_install_intent_in_process,
            args=(
                str(tmp_path),
                control.run_id,
                str(work_dir),
                ready,
                start,
                results,
            ),
        )
        for _index in range(2)
    ]
    for process in processes:
        process.start()
    assert [ready.get(timeout=5) for _process in processes] == [True, True]
    start.set()
    for process in processes:
        process.join(timeout=5)
        assert process.exitcode == 0

    assert sorted(results.get(timeout=5) for _process in processes) == [
        False,
        True,
    ]


def test_exception_after_durable_receipt_recovers_without_resubmit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    control = _control(tmp_path)
    original_write_receipt = psij_module.write_receipt

    def install_then_raise(*args: Any, **kwargs: Any) -> Any:
        original_write_receipt(*args, **kwargs)
        raise OSError("interrupted after durable receipt")

    monkeypatch.setattr(psij_module, "write_receipt", install_then_raise)

    result = launch_psij(control, _launch_config())

    assert result.native_id == "job-1"
    assert _Executor.submit_count == 1
    assert (control.control_dir / "psij_job.json").is_file()


def test_uncertain_submit_stays_nonterminal_if_progress_is_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    control = _control(tmp_path)
    original_submit = _Executor.submit

    def accept_then_raise(self: _Executor, job: _Job) -> None:
        original_submit(self, job)
        raise RuntimeError("submit response was lost")

    monkeypatch.setattr(_Executor, "submit", accept_then_raise)
    monkeypatch.setattr(
        psij_module,
        "_append_observation",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            OSError("progress unavailable")
        ),
    )

    with pytest.raises(PSIJSubmissionUncertainError):
        launch_psij(control, _launch_config())

    assert control.read_status()["state"] == "prepared"
    assert _Executor.submit_count == 1
    assert not (control.control_dir / "psij_job.json").exists()


def test_post_receipt_progress_failure_does_not_fail_launch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    control = _control(tmp_path)
    monkeypatch.setattr(
        psij_module,
        "_append_observation",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            OSError("progress unavailable")
        ),
    )

    result = launch_psij(control, _launch_config())

    assert result.native_id == "job-1"
    assert _Executor.submit_count == 1
    assert (control.control_dir / "psij_job.json").is_file()


def test_reconnect_rejects_an_attached_native_id_mismatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    control = _control(tmp_path)
    launch_psij(control, _launch_config())
    original_attach = _Executor.attach

    def mismatched_attach(
        self: _Executor,
        job: _Job,
        native_id: str,
    ) -> None:
        original_attach(self, job, native_id)
        job.native_id = "different-job"

    monkeypatch.setattr(_Executor, "attach", mismatched_attach)

    with pytest.raises(LauncherProtocolError, match="different native"):
        launch_psij(control, _launch_config())

    assert _Executor.submit_count == 1


def test_reconnect_rejects_a_missing_attached_native_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    control = _control(tmp_path)
    launch_psij(control, _launch_config())
    monkeypatch.setattr(
        _Executor,
        "attach",
        lambda self, job, native_id: setattr(job, "native_id", None),
    )

    with pytest.raises(LauncherProtocolError, match="did not expose"):
        launch_psij(control, _launch_config())

    assert _Executor.submit_count == 1


def test_reconnect_tolerates_transient_attach_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    control = _control(tmp_path)
    launch_psij(control, _launch_config())
    monkeypatch.setattr(
        _Executor,
        "attach",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            RuntimeError("scheduler temporarily unavailable")
        ),
    )

    result = launch_psij(control, _launch_config())

    assert result.native_id == "job-1"
    assert _Executor.submit_count == 1


def test_reconnect_uses_receipt_after_job_work_directory_is_removed(
    tmp_path: Path,
) -> None:
    job_work_dir = tmp_path / "job-work"
    job_work_dir.mkdir()
    launch = _launch_config(work_dir=PurePosixPath(str(job_work_dir)))
    control = _control(tmp_path, launch=launch)
    launch_psij(control, launch)
    job_work_dir.rmdir()

    result = launch_psij(control, launch)

    assert result.native_id == "job-1"
    assert _Executor.submit_count == 1


def test_terminal_job_recovers_an_expired_claim_without_rerun(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    control = _control(tmp_path)
    launch_psij(control, _launch_config())
    claimed = control.claim_start(
        expected_revision=0,
        owner="dead-orchestrator",
        backend="psij",
        lease_seconds=0.01,
    )
    control.transition(
        expected_revision=claimed.status["revision"],
        expected_claim_epoch=claimed.claim["epoch"],
        new_state="running",
    )
    _Executor.states["job-1"] = _JobState.FAILED
    time.sleep(0.02)
    monkeypatch.setattr(
        "bioimageflow.launcher.orchestrator._prepare_execution",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("recovery must not rerun workflow code")
        ),
    )

    psij_module.reconcile_psij(control)

    assert control.read_status()["state"] == "lost"
    error = json.loads((control.control_dir / "error.json").read_text())
    assert error["code"] == "orchestrator-lost"
