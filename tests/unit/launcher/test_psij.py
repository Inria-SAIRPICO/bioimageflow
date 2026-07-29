from __future__ import annotations

import json
import subprocess
import sys
from datetime import timedelta
from enum import Enum
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from typing import Any, Literal

import pytest

import bioimageflow.launcher.psij as psij_module
from bioimageflow.launcher import (
    BackendNotSupportedError,
    LauncherProtocolError,
    PSIJLaunchConfig,
    PSIJSubmissionUncertainError,
    WorkflowRun,
)
from bioimageflow.launcher.backends import launch_orchestrator
from bioimageflow.launcher.control import LauncherRunControl
from bioimageflow.launcher.psij import (
    PSIJLaunch,
    cancel_psij,
    launch_psij,
    observe_psij,
    reconcile_psij,
)
from bioimageflow.launcher.repository import LauncherRepository
from bioimageflow.launcher.run import _hard_terminate_after_grace
from tests.unit.launcher.helpers import launcher_submission


_ORIGINAL_LOAD_RUNTIME = psij_module._load_runtime


class _JobState(Enum):
    NEW = "NEW"
    QUEUED = "QUEUED"
    ACTIVE = "ACTIVE"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELED = "CANCELED"


class _Status:
    def __init__(self, state: _JobState) -> None:
        self.state = state


class _Record:
    def __init__(self, **values: Any) -> None:
        self.__dict__.update(values)


class _Job:
    def __init__(self, spec: Any = None) -> None:
        self.spec = spec
        self.native_id: str | None = None
        self.status = _Status(_JobState.NEW)
        self._executor: _Executor | None = None

    def wait(
        self,
        timeout: timedelta | None = None,
        target_states: Any = None,
    ) -> _Status:
        return self.status

    def cancel(self) -> None:
        assert self._executor is not None
        assert self.native_id is not None
        self._executor.states[self.native_id] = _JobState.CANCELED
        self.status = _Status(_JobState.CANCELED)


class _Executor:
    states: dict[str, _JobState] = {}
    specs: list[Any] = []
    submit_count = 0
    instances: list["_Executor"] = []

    def __init__(self, name: str, config: Any) -> None:
        self.name = name
        self.config = config
        self.instances.append(self)

    def submit(self, job: _Job) -> None:
        type(self).submit_count += 1
        native_id = f"job-{type(self).submit_count}"
        job.native_id = native_id
        job._executor = self
        job.status = _Status(_JobState.QUEUED)
        self.states[native_id] = _JobState.QUEUED
        self.specs.append(job.spec)

    def attach(self, job: _Job, native_id: str) -> None:
        job.native_id = native_id
        job._executor = self
        job.status = _Status(self.states[native_id])


class _JobExecutor:
    names = {"slurm", "pbs", "lsf"}

    @classmethod
    def get_executor_names(cls) -> set[str]:
        return set(cls.names)

    @classmethod
    def get_instance(cls, name: str, config: Any = None) -> _Executor:
        return _Executor(name, config)


def _runtime() -> Any:
    return SimpleNamespace(
        Job=_Job,
        JobState=_JobState,
        JobSpec=_Record,
        JobAttributes=_Record,
        ResourceSpecV1=_Record,
        JobExecutor=_JobExecutor,
        JobExecutorConfig=_Record,
    )


@pytest.fixture(autouse=True)
def _reset_fake_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    _Executor.states = {}
    _Executor.specs = []
    _Executor.submit_count = 0
    _Executor.instances = []
    _JobExecutor.names = {"slurm", "pbs", "lsf"}
    monkeypatch.setattr(psij_module, "_load_runtime", _runtime)


def _launch_config(
    *,
    executor: Literal["slurm", "pbs", "lsf"] = "slurm",
    hard_cancel_after: float | None = None,
    work_dir: PurePosixPath | None = None,
) -> PSIJLaunchConfig:
    return PSIJLaunchConfig(
        executor=executor,
        walltime=timedelta(minutes=45),
        queue="cpu",
        project="BIOIMAGE",
        cpu_cores=4,
        work_dir=work_dir,
        hard_cancel_after=hard_cancel_after,
    )


def _control(
    tmp_path: Path,
    *,
    launch: PSIJLaunchConfig | None = None,
) -> LauncherRunControl:
    repository = LauncherRepository(tmp_path)
    run_id = repository.new_run_id()
    submission = launcher_submission(repository.storage_root, run_id)
    submission["launch"] = (launch or _launch_config()).to_dict()
    return repository.allocate(submission, backend="psij")


def test_psij_backend_builds_exact_single_process_job_and_receipt(
    tmp_path: Path,
) -> None:
    job_work_dir = tmp_path / "job-work"
    job_work_dir.mkdir()
    launch = _launch_config(work_dir=PurePosixPath(str(job_work_dir)))
    control = _control(tmp_path, launch=launch)

    result = launch_orchestrator(control, launch)

    assert isinstance(result, PSIJLaunch)
    assert result.native_id == "job-1"
    assert _Executor.submit_count == 1
    spec = _Executor.specs[0]
    assert Path(spec.executable).is_absolute()
    assert spec.arguments == [
        "-m",
        "bioimageflow.launcher.orchestrator",
        "--storage-root",
        str(tmp_path.resolve()),
        "--run-id",
        control.run_id,
    ]
    assert spec.directory == str(job_work_dir)
    assert spec.stdout_path == control.control_dir / "logs/orchestrator.out"
    assert spec.stderr_path == control.control_dir / "logs/orchestrator.err"
    assert spec.resources.__dict__ == {
        "node_count": 1,
        "process_count": 1,
        "processes_per_node": 1,
        "cpu_cores_per_process": 4,
    }
    assert spec.attributes.duration == timedelta(minutes=45)
    assert spec.attributes.queue_name == "cpu"
    assert spec.attributes.account == "BIOIMAGE"
    assert _Executor.instances[0].config.work_directory == (
        control.control_dir / "psij/executor"
    )
    assert (control.control_dir / "psij_intent.json").is_file()
    receipt = json.loads((control.control_dir / "psij_job.json").read_text())
    assert receipt["native_id"] == "job-1"
    assert receipt["run_id"] == control.run_id
    assert receipt["submit_token"] in (
        control.control_dir / "psij_intent.json"
    ).read_text()
    assert control.read_status()["state"] == "prepared"


def test_psij_import_is_lazy_for_ordinary_public_import() -> None:
    code = (
        "import sys; import bioimageflow; "
        "assert 'psij' not in sys.modules; "
        "import bioimageflow.launcher.backends; "
        "assert 'psij' not in sys.modules"
    )

    subprocess.run([sys.executable, "-c", code], check=True)


def test_missing_optional_runtime_names_install_extra(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        psij_module.importlib,
        "import_module",
        lambda name: (_ for _ in ()).throw(ModuleNotFoundError(name)),
    )

    with pytest.raises(BackendNotSupportedError, match=r"bioimageflow\[psij\]"):
        _ORIGINAL_LOAD_RUNTIME()


def test_missing_executor_descriptor_fails_before_intent_or_submit(
    tmp_path: Path,
) -> None:
    control = _control(tmp_path, launch=_launch_config(executor="pbs"))
    _JobExecutor.names.remove("pbs")

    with pytest.raises(BackendNotSupportedError, match="descriptor"):
        launch_psij(control, _launch_config(executor="pbs"))

    assert _Executor.submit_count == 0
    assert not (control.control_dir / "psij_intent.json").exists()


def test_crash_after_submit_before_receipt_is_never_resubmitted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    control = _control(tmp_path)
    monkeypatch.setattr(
        psij_module,
        "write_receipt",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            OSError("receipt crash")
        ),
    )

    with pytest.raises(PSIJSubmissionUncertainError) as first:
        launch_psij(control, _launch_config())

    assert first.value.details["run_id"] == control.run_id
    assert _Executor.submit_count == 1
    assert control.read_status()["state"] == "prepared"
    assert not (control.control_dir / "psij_job.json").exists()

    with pytest.raises(PSIJSubmissionUncertainError):
        launch_psij(control, _launch_config())

    assert _Executor.submit_count == 1
    assert control.read_status()["state"] == "prepared"
    assert [
        event["payload"]["event"] for event in control.read_progress()
    ] == ["psij_submission_uncertain"]


def test_crash_before_intent_never_calls_external_submit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    control = _control(tmp_path)
    monkeypatch.setattr(
        psij_module,
        "install_intent",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            OSError("intent crash")
        ),
    )

    with pytest.raises(OSError, match="intent crash"):
        launch_psij(control, _launch_config())

    assert _Executor.submit_count == 0


def test_receipt_attach_after_live_launcher_objects_are_discarded(
    tmp_path: Path,
) -> None:
    control = _control(tmp_path)
    launch_psij(control, _launch_config())
    _Executor.instances = []

    observation = observe_psij(control)

    assert observation.native_id == "job-1"
    assert observation.executor == "slurm"
    assert observation.state == "QUEUED"
    assert _Executor.instances[-1].config.work_directory == (
        control.control_dir / "psij/executor"
    )
    assert _Executor.submit_count == 1


def test_queued_and_active_scheduler_states_remain_prepared(
    tmp_path: Path,
) -> None:
    control = _control(tmp_path)
    launch_psij(control, _launch_config())
    run = WorkflowRun.open(tmp_path, control.run_id)

    run.refresh()
    assert run.status == "prepared"

    _Executor.states["job-1"] = _JobState.ACTIVE
    run.refresh()

    assert run.status == "prepared"
    assert [event["payload"]["event"] for event in control.read_progress()] == [
        "psij_queued",
        "psij_active",
    ]


@pytest.mark.parametrize(
    "state",
    [_JobState.COMPLETED, _JobState.FAILED, _JobState.CANCELED],
)
def test_terminal_scheduler_state_before_claim_fails_run(
    tmp_path: Path,
    state: _JobState,
) -> None:
    control = _control(tmp_path)
    launch_psij(control, _launch_config())
    _Executor.states["job-1"] = state

    reconcile_psij(control)

    status = control.read_status()
    assert status["state"] == "failed"
    error = json.loads((control.control_dir / "error.json").read_text())
    assert error["code"] == "psij-job-terminal-before-claim"
    assert error["backend"] == {"name": "psij"}


def test_terminal_scheduler_state_after_claim_is_secondary_only(
    tmp_path: Path,
) -> None:
    control = _control(tmp_path)
    launch_psij(control, _launch_config())
    claimed = control.claim_start(
        expected_revision=0,
        owner="orchestrator",
        backend="psij:job-1",
        lease_seconds=30,
    )
    control.transition(
        expected_revision=claimed.status["revision"],
        expected_claim_epoch=claimed.claim["epoch"],
        new_state="running",
    )
    _Executor.states["job-1"] = _JobState.FAILED

    reconcile_psij(control)

    assert control.read_status()["state"] == "running"


def test_prepared_workflow_run_cancel_cancels_exact_queued_job(
    tmp_path: Path,
) -> None:
    control = _control(tmp_path)
    launch_psij(control, _launch_config())
    run = WorkflowRun.open(tmp_path, control.run_id)

    run.cancel()

    assert run.status == "cancelled"
    assert _Executor.states == {"job-1": _JobState.CANCELED}
    assert _Executor.submit_count == 1


def test_active_hard_cancel_confirms_psij_termination_then_marks_lost(
    tmp_path: Path,
) -> None:
    control = _control(
        tmp_path,
        launch=_launch_config(hard_cancel_after=0.001),
    )
    launch_psij(control, _launch_config(hard_cancel_after=0.001))
    claimed = control.claim_start(
        expected_revision=0,
        owner="orchestrator",
        backend="psij:job-1",
        lease_seconds=30,
    )
    control.transition(
        expected_revision=claimed.status["revision"],
        expected_claim_epoch=claimed.claim["epoch"],
        new_state="running",
    )
    run = WorkflowRun.open(tmp_path, control.run_id)
    run.cancel()

    _hard_terminate_after_grace(
        control,
        storage_path=tmp_path.resolve(),
        grace_seconds=0.001,
    )

    assert _Executor.states == {"job-1": _JobState.CANCELED}
    assert control.read_status()["state"] == "lost"
    error = json.loads((control.control_dir / "error.json").read_text())
    assert error["code"] == "orchestrator-hard-terminated"
    assert error["backend"] == {"name": "psij"}


def test_cancel_without_receipt_resolves_uncertain_prepared_run(
    tmp_path: Path,
) -> None:
    control = _control(tmp_path)
    work_dir = control.confined_path("psij/executor")
    work_dir.mkdir(parents=True)
    from bioimageflow.launcher.psij_artifacts import install_intent

    _intent, created = install_intent(
        control,
        _launch_config(),
        work_dir,
    )
    assert created
    run = WorkflowRun.open(tmp_path, control.run_id)

    run.cancel()

    assert run.status == "cancelled"
    assert _Executor.submit_count == 0


def test_cancel_rejects_receipt_with_wrong_native_id_correlation(
    tmp_path: Path,
) -> None:
    control = _control(tmp_path)
    launch_psij(control, _launch_config())
    receipt_path = control.control_dir / "psij_job.json"
    receipt = json.loads(receipt_path.read_text())
    receipt["native_id"] = " job-1"
    receipt_path.write_text(json.dumps(receipt))

    with pytest.raises(LauncherProtocolError, match="native job ID"):
        cancel_psij(control)


def test_prepared_cancel_is_best_effort_without_psij_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    control = _control(tmp_path)
    launch_psij(control, _launch_config())
    run = WorkflowRun.open(tmp_path, control.run_id)
    monkeypatch.setattr(
        psij_module,
        "_load_runtime",
        lambda: (_ for _ in ()).throw(
            BackendNotSupportedError("runtime unavailable")
        ),
    )

    run.cancel()

    assert run.status == "cancelled"
