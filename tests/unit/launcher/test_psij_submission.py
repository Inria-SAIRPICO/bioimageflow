from __future__ import annotations

from pathlib import Path

import pytest

import bioimageflow.launcher.psij as psij_module
from bioimageflow import PSIJSubmissionUncertainError, Workflow
from bioimageflow.launcher import WorkflowRun
from bioimageflow.launcher.submission import submit_workflow
from tests.unit.launcher.test_psij import (
    _Executor,
    _JobExecutor,
    _launch_config,
    _runtime,
)
from tests.unit.launcher.test_submission_api import _binding, _config_ref


@pytest.fixture(autouse=True)
def _fake_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    _Executor.states = {}
    _Executor.specs = []
    _Executor.submit_count = 0
    _Executor.instances = []
    _JobExecutor.names = {"slurm", "pbs", "lsf"}
    monkeypatch.setattr(psij_module, "_load_runtime", _runtime)


def test_public_submission_dispatches_one_psij_job(tmp_path: Path) -> None:
    workflow = Workflow(storage_path=tmp_path, engine="direct")

    run = submit_workflow(
        workflow,
        parsl_config=_config_ref(),
        executor_bindings={"threads": _binding()},
        launch=_launch_config(),
    )

    assert isinstance(run, WorkflowRun)
    assert run.status == "prepared"
    assert _Executor.submit_count == 1
    assert (run.control_dir / "psij_intent.json").is_file()
    assert (run.control_dir / "psij_job.json").is_file()


def test_public_submission_preserves_prepared_uncertainty(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow = Workflow(storage_path=tmp_path, engine="direct")
    monkeypatch.setattr(
        psij_module,
        "write_receipt",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            OSError("receipt crash")
        ),
    )

    with pytest.raises(PSIJSubmissionUncertainError) as captured:
        submit_workflow(
            workflow,
            parsl_config=_config_ref(),
            executor_bindings={"threads": _binding()},
            launch=_launch_config(),
        )

    run_id = captured.value.details["run_id"]
    run = WorkflowRun.open(tmp_path, run_id)
    assert run.status == "prepared"
    assert _Executor.submit_count == 1
    assert not (run.control_dir / "error.json").exists()


def test_terminal_run_refresh_does_not_require_psij_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow = Workflow(storage_path=tmp_path, engine="direct")
    run = submit_workflow(
        workflow,
        parsl_config=_config_ref(),
        executor_bindings={"threads": _binding()},
        launch=_launch_config(),
    )
    run.cancel()
    monkeypatch.setattr(
        psij_module,
        "_load_runtime",
        lambda: (_ for _ in ()).throw(
            AssertionError("terminal refresh loaded PSI/J")
        ),
    )

    run.refresh()

    assert run.status == "cancelled"
