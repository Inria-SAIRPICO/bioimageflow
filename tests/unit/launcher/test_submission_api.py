from pathlib import Path

import pytest

from bioimageflow import (
    ExecutorBinding,
    ExecutorCapabilities,
    ParslTaskPolicy,
    WorkerEnvironmentAttestation,
    WorkerSlotCapacity,
    Workflow,
)
from bioimageflow.launcher.errors import BackendNotSupportedError
from bioimageflow.launcher.run import WorkflowRun
from bioimageflow.launcher.submission import submit_workflow
from bioimageflow.launcher.types import (
    OrchestratorLaunchConfig,
    ParslConfigRef,
)


def _binding() -> ExecutorBinding:
    return ExecutorBinding(
        label="threads",
        environments=(
            WorkerEnvironmentAttestation(
                name="default",
                dependency_hash="0" * 64,
                allow_flexible_versions=False,
                core_requirement="bioimageflow-core==0.1.7",
            ),
        ),
        capabilities=ExecutorCapabilities(
            storage_modes=("shared_fs",),
            tool_origin_modes=("installed_module",),
            slot=WorkerSlotCapacity(cpu=1),
        ),
    )


def _config_ref() -> ParslConfigRef:
    return ParslConfigRef(
        "tests.unit.launcher.config_factories:build",
        {"workers": 1},
    )


def test_manual_submission_persists_runtime_storage_outside_graph(
    tmp_path: Path,
) -> None:
    workflow = Workflow(storage_path=tmp_path, engine="direct")

    run = submit_workflow(
        workflow,
        parsl_config=_config_ref(),
        executor_bindings={"threads": _binding()},
        launch=OrchestratorLaunchConfig(backend="manual"),
        task_policy=ParslTaskPolicy(max_in_flight=2),
    )

    assert isinstance(run, WorkflowRun)
    assert run.status == "prepared"
    submission = run._control.read_submission()
    assert submission["storage_root"] == str(tmp_path.resolve())
    assert "storage_path" not in submission["workflow"]["payload"]["config"]
    assert (run.control_dir / "command.json").is_file()


def test_unsupported_backend_fails_before_allocation(tmp_path: Path) -> None:
    workflow = Workflow(storage_path=tmp_path, engine="direct")

    with pytest.raises(BackendNotSupportedError) as captured:
        submit_workflow(
            workflow,
            parsl_config=_config_ref(),
            executor_bindings={"threads": _binding()},
            launch=OrchestratorLaunchConfig(backend="slurm"),
        )

    assert captured.value.code == "backend-not-supported"
    assert not (tmp_path / "launcher").exists()


def test_invalid_route_fails_before_allocation(tmp_path: Path) -> None:
    workflow = Workflow(storage_path=tmp_path, engine="direct")

    with pytest.raises(ValueError, match="unknown executor"):
        submit_workflow(
            workflow,
            parsl_config=_config_ref(),
            executor_bindings={"threads": _binding()},
            node_routes={"node": "missing"},
            launch=OrchestratorLaunchConfig(backend="manual"),
        )

    assert not (tmp_path / "launcher").exists()
