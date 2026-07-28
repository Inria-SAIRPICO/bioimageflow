import subprocess
import sys
import time
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
from bioimageflow.storage import Storage
from tests.unit.launcher.helpers import (
    launcher_submission,
    public_progress_payload,
)


RUN_ID = "run_1234567812344abc923456789abcdef0"


def _submission(
    tmp_path: Path,
    *,
    backend: str = "manual",
    hard_cancel_after: float | None = None,
) -> dict[str, object]:
    return launcher_submission(
        tmp_path,
        RUN_ID,
        backend=backend,
        hard_cancel_after=hard_cancel_after,
    )


def _run(tmp_path: Path) -> tuple[WorkflowRun, object]:
    control = LauncherRepository(tmp_path).allocate(
        _submission(tmp_path),
        backend="manual",
    )
    return WorkflowRun.open(tmp_path, RUN_ID), control


def test_open_refresh_progress_and_prepared_cancel(tmp_path: Path) -> None:
    run, control = _run(tmp_path)
    control.append_progress(kind="public", payload=public_progress_payload())

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
            return -15 if self.terminated else None

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


def test_reconnected_hard_cancel_uses_persisted_process_identity(
    tmp_path: Path,
) -> None:
    from bioimageflow.launcher.artifacts import write_local_process_identity
    from bioimageflow.launcher.backends import _process_start_token

    control = LauncherRepository(tmp_path).allocate(
        _submission(
            tmp_path,
            backend="local",
            hard_cancel_after=0.01,
        ),
        backend="local",
    )
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        start_new_session=True,
    )
    try:
        token = _process_start_token(process.pid)
        assert token is not None
        write_local_process_identity(
            control,
            pid=process.pid,
            start_token=token,
        )
        claimed = control.claim_start(
            expected_revision=0,
            owner="detached-local-owner",
            backend="local",
            lease_seconds=30,
        )
        control.transition(
            expected_revision=claimed.status["revision"],
            expected_claim_epoch=claimed.claim["epoch"],
            new_state="running",
        )
        Storage(tmp_path).write_run_metadata(
            RUN_ID,
            workflow_identity="workflow:test",
            engine="parsl:parallel",
            status="running",
            target_nodes=[],
        )

        reconnected = WorkflowRun.open(tmp_path, RUN_ID)
        reconnected.cancel()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            reconnected.refresh()
            if reconnected.status == "lost":
                break
            time.sleep(0.01)

        assert reconnected.status == "lost"
        assert process.wait(timeout=2) != 0
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=2)
