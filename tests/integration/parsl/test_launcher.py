from __future__ import annotations

import time
from pathlib import Path

import pandas as pd

from bioimageflow import (
    ExecutorBinding,
    ExecutorCapabilities,
    OrchestratorLaunchConfig,
    ParslConfigRef,
    WorkerEnvironmentAttestation,
    WorkerSlotCapacity,
    Workflow,
    WorkflowRun,
    submit_workflow,
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


def test_local_launcher_runs_in_separate_process_and_reconnects(
    tmp_path: Path,
) -> None:
    workflow = Workflow(storage_path=tmp_path, engine="direct")
    run = submit_workflow(
        workflow,
        parsl_config=ParslConfigRef(
            "tests.unit.launcher.config_factories:build_threads",
            {"max_threads": 1},
        ),
        executor_bindings={"threads": _binding()},
        launch=OrchestratorLaunchConfig(backend="local"),
    )

    deadline = time.monotonic() + 15
    while run.status not in {"succeeded", "failed", "cancelled", "lost"}:
        if time.monotonic() >= deadline:
            raise AssertionError(
                f"Local orchestrator timed out in {run.status!r}.\n{run.logs()}"
            )
        time.sleep(0.02)
        run.refresh()

    assert run.status == "succeeded", run.logs()
    assert run.control_dir == tmp_path / "launcher" / "v1" / "runs" / run.id
    assert run.view_dir == tmp_path / "views" / "runs" / run.id
    reopened = WorkflowRun.open(tmp_path, run.id)
    result = reopened.result()
    assert isinstance(result, pd.DataFrame)
    assert result.empty
