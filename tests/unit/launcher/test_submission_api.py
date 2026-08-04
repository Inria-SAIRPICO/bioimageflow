from datetime import timedelta
from pathlib import Path, PurePosixPath

import pytest

from bioimageflow import (
    ExecutorBinding,
    ExecutorCapabilities,
    PSIJLaunchConfig,
    ParslTaskPolicy,
    PreLaunchScript,
    RemoteWorkflowRun,
    SSHSubmissionTransport,
    WorkerEnvironmentAttestation,
    WorkerSlotCapacity,
    Workflow,
)
from bioimageflow.launcher.run import WorkflowRun
from bioimageflow.launcher.submission import submit_workflow
from bioimageflow.launcher.types import (
    OrchestratorLaunchConfig,
    ParslConfigRef,
)
import bioimageflow.launcher.ssh as ssh_module


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


def test_direct_scheduler_backend_alias_is_rejected_before_allocation(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="Unknown launcher backend"):
        OrchestratorLaunchConfig(backend="slurm")  # type: ignore[arg-type]

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


def test_transport_submission_returns_remote_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow = Workflow(storage_path=tmp_path, engine="direct")
    transport = SSHSubmissionTransport(
        host="hpc",
        staging_root=PurePosixPath("/cluster/staging"),
        remote_executable=PurePosixPath("/cluster/bin/agent"),
    )
    run_id = "run_1234567812344abc923456789abcdef0"
    submitted: dict = {}

    def fake_submit(*args, **kwargs):
        del args
        submitted.update(kwargs)
        return run_id

    monkeypatch.setattr(
        ssh_module,
        "submit_cluster_workflow",
        fake_submit,
    )
    monkeypatch.setattr(
        ssh_module,
        "execute_cluster_command",
        lambda *args, **kwargs: {
            "schema": "bioimageflow.launcher.run-observation.v1",
            "error": None,
            "retry_plan": None,
            "run_id": run_id,
            "state": "prepared",
            "status_revision": 0,
            "storage_path": workflow.storage_path.as_posix(),
            "terminal": False,
            "updated_at": "2026-07-29T12:00:00Z",
        },
    )

    run = submit_workflow(
        workflow,
        parsl_config=_config_ref(),
        executor_bindings={"threads": _binding()},
        launch=PSIJLaunchConfig(
            executor="slurm",
            walltime=timedelta(minutes=10),
        ),
        transport=transport,
        node_input_overrides={"files": {"path": Path("/cluster/images")}},
    )

    assert isinstance(run, RemoteWorkflowRun)
    assert run.id == run_id
    assert submitted["node_input_overrides"] == {
        "files": {"path": Path("/cluster/images")}
    }
    assert not (tmp_path / "launcher").exists()


def test_transport_submission_requires_psij_launch(tmp_path: Path) -> None:
    workflow = Workflow(storage_path=tmp_path, engine="direct")
    transport = SSHSubmissionTransport(
        host="hpc",
        staging_root=PurePosixPath("/cluster/staging"),
        remote_executable=PurePosixPath("/cluster/bin/agent"),
    )

    with pytest.raises(TypeError, match="PSIJLaunchConfig"):
        submit_workflow(
            workflow,
            parsl_config=_config_ref(),
            executor_bindings={"threads": _binding()},
            launch=OrchestratorLaunchConfig(backend="manual"),
            transport=transport,
        )


def test_pre_launch_requires_psij_and_cluster_file_requires_transport(
    tmp_path: Path,
) -> None:
    workflow = Workflow(storage_path=tmp_path, engine="direct")

    with pytest.raises(ValueError, match="PSIJLaunchConfig"):
        submit_workflow(
            workflow,
            parsl_config=_config_ref(),
            executor_bindings={"threads": _binding()},
            launch=OrchestratorLaunchConfig(backend="manual"),
            pre_launch=PreLaunchScript.from_text("echo ready\n"),
        )

    with pytest.raises(ValueError, match="transported"):
        submit_workflow(
            workflow,
            parsl_config=_config_ref(),
            executor_bindings={"threads": _binding()},
            launch=PSIJLaunchConfig(
                executor="slurm",
                walltime=timedelta(minutes=10),
            ),
            pre_launch=PreLaunchScript.from_cluster_file("/shared/init.sh"),
        )

    assert not (tmp_path / "launcher").exists()


def test_node_input_overrides_require_transported_submission(tmp_path: Path) -> None:
    workflow = Workflow(storage_path=tmp_path, engine="direct")

    with pytest.raises(ValueError, match="transported submission"):
        submit_workflow(
            workflow,
            parsl_config=_config_ref(),
            executor_bindings={"threads": _binding()},
            launch=OrchestratorLaunchConfig(backend="manual"),
            node_input_overrides={
                "files": {"path": Path("/cluster/images")},
            },
        )

    assert not (tmp_path / "launcher").exists()
