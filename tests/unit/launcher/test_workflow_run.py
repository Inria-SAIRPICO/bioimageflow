from pathlib import Path
import time

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
from bioimageflow.storage import Storage


RUN_ID = "run_1234567812344abc923456789abcdef0"


def _submission(
    tmp_path: Path,
    *,
    backend: str = "manual",
    hard_cancel_after: float | None = None,
) -> dict[str, object]:
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
            "hard_cancel_after": hard_cancel_after,
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


def test_local_hard_cancel_terminates_tracked_process_and_marks_lost(
    tmp_path: Path,
) -> None:
    from bioimageflow.launcher.backends import _track_local_process

    class FakeProcess:
        def __init__(self) -> None:
            self.terminated = False

        def poll(self) -> int | None:
            return None

        def terminate(self) -> None:
            self.terminated = True

    control = LauncherRepository(tmp_path).allocate(
        _submission(
            tmp_path,
            backend="local",
            hard_cancel_after=0.01,
        ),
        backend="local",
    )
    process = FakeProcess()
    _track_local_process(control, process)  # type: ignore[arg-type]
    status = control.read_status()
    starting = control.claim_start(
        expected_revision=status["revision"],
        owner="local-owner",
        backend="local",
        lease_seconds=30,
    )
    control.transition(
        expected_revision=starting.status["revision"],
        expected_claim_epoch=starting.claim["epoch"],
        new_state="running",
    )
    Storage(tmp_path).write_run_metadata(
        RUN_ID,
        workflow_identity="workflow:test",
        engine="parsl:parallel",
        status="running",
        target_nodes=[],
    )
    run = WorkflowRun(control)

    run.cancel()
    deadline = time.monotonic() + 2
    while True:
        run.refresh()
        if run.status == "lost":
            break
        if time.monotonic() >= deadline:
            raise AssertionError(f"Hard cancellation remained {run.status!r}.")
        time.sleep(0.005)

    status = control.read_status()
    assert process.terminated is True
    assert status["hard_termination_requested"] is True
    assert status["error"] == "error.json"
    assert Storage(tmp_path)._load_run_metadata(RUN_ID)["status"] == "failed"
