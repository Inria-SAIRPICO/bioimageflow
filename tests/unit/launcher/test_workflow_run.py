from pathlib import Path

import pandas as pd
import pytest

from bioimageflow.engine import WorkflowCancelledError
from bioimageflow.launcher.artifacts import write_error
from bioimageflow.launcher.errors import (
    WorkflowRunFailedError,
    WorkflowRunNotReadyError,
)
from bioimageflow.launcher.repository import LauncherRepository
from bioimageflow.launcher.returns import persist_public_return
from bioimageflow.launcher.run import WorkflowRun
from bioimageflow.launcher.schemas import SUBMISSION_SCHEMA, utc_timestamp


RUN_ID = "run_1234567812344abc923456789abcdef0"


def _submission(tmp_path: Path, *, backend: str = "manual") -> dict[str, object]:
    return {
        "schema": SUBMISSION_SCHEMA,
        "run_id": RUN_ID,
        "created_at": utc_timestamp(),
        "storage_root": str(tmp_path.resolve()),
        "canonical_view": f"views/runs/{RUN_ID}",
        "workflow": {
            "kind": "graph_v1",
            "digest": "sha256:" + "0" * 64,
            "payload": {},
        },
        "invocation": {},
        "parsl_config": {},
        "executor_bindings": {},
        "node_routes": None,
        "environment_routes": None,
        "shared_runtime_root": None,
        "task_policy": {},
        "launch": {
            "backend": backend,
            "work_dir": None,
            "hard_cancel_after": None,
        },
        "protocol_versions": {},
    }


def _run(tmp_path: Path) -> tuple[WorkflowRun, object]:
    control = LauncherRepository(tmp_path).allocate(
        _submission(tmp_path),
        backend="manual",
    )
    return WorkflowRun.open(tmp_path, RUN_ID), control


def test_open_refresh_progress_and_prepared_cancel(tmp_path: Path) -> None:
    run, control = _run(tmp_path)
    control.append_progress(kind="public", payload={"status": "started"})

    assert run.status == "prepared"
    assert [event["sequence"] for event in run.progress()] == [1]
    run.cancel()
    run.refresh()

    assert run.status == "cancelled"
    with pytest.raises(WorkflowCancelledError):
        run.result()


def test_nonterminal_result_is_not_ready(tmp_path: Path) -> None:
    run, _control = _run(tmp_path)

    with pytest.raises(WorkflowRunNotReadyError) as captured:
        run.result()

    assert captured.value.code == "workflow-run-not-ready"


def test_failed_result_carries_persisted_error(tmp_path: Path) -> None:
    run, control = _run(tmp_path)
    error = ValueError("broken workflow")
    write_error(control, code="workflow-failed", error=error)
    status = control.read_status()
    control.transition(
        expected_revision=status["revision"],
        new_state="failed",
        updates={"error": "error.json"},
    )

    with pytest.raises(WorkflowRunFailedError) as captured:
        run.result()

    assert captured.value.error["code"] == "workflow-failed"


def test_successful_result_rehydrates_return(tmp_path: Path) -> None:
    run, control = _run(tmp_path)
    persist_public_return(
        control.control_dir,
        tmp_path,
        RUN_ID,
        pd.DataFrame({"value": [7]}, index=["row"]),
        outcomes=(),
    )
    status = control.read_status()
    starting = control.claim_start(
        expected_revision=status["revision"],
        owner="test-owner",
        backend="manual",
        lease_seconds=30,
    )
    running = control.transition(
        expected_revision=starting.status["revision"],
        expected_claim_epoch=starting.claim["epoch"],
        new_state="running",
    )
    finalizing = control.transition(
        expected_revision=running["revision"],
        expected_claim_epoch=starting.claim["epoch"],
        new_state="finalizing",
    )
    control.transition(
        expected_revision=finalizing["revision"],
        expected_claim_epoch=starting.claim["epoch"],
        new_state="succeeded",
    )

    result = run.result()

    assert result.at["row", "value"] == 7
