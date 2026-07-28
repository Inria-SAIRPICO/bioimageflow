"""Reconnectable public handle for one submitted workflow run."""

from __future__ import annotations

import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from bioimageflow.engine import WorkflowCancelledError
from bioimageflow.storage import Storage

from .artifacts import build_error_payload, read_error
from .errors import (
    WorkflowRunFailedError,
    WorkflowRunLostError,
    WorkflowRunNotReadyError,
)
from .control import LauncherRunControl
from .repository import LauncherRepository
from .returns import load_public_return
from .state import RevisionConflictError
from .types import OrchestratorLaunchConfig


def _hard_terminate_after_grace(
    control: LauncherRunControl,
    *,
    storage_path: Path,
    grace_seconds: float,
) -> None:
    status = control.read_status()
    requested_at = status.get("cancel_requested_at")
    if status["state"] != "cancel_requested" or not isinstance(
        requested_at,
        str,
    ):
        return
    try:
        requested = datetime.fromisoformat(
            requested_at.replace("Z", "+00:00")
        ).astimezone(timezone.utc)
    except ValueError:
        return
    remaining = (
        requested.timestamp() + grace_seconds - datetime.now(timezone.utc).timestamp()
    )
    if remaining > 0 and threading.Event().wait(remaining):
        return
    status = control.read_status()
    if status["state"] != "cancel_requested" or status["backend"] != "local":
        return
    from .backends import terminate_local_orchestrator

    if not terminate_local_orchestrator(control):
        return
    error = WorkflowRunLostError(
        "The local orchestrator was terminated after its cancellation grace period.",
        details={"run_id": control.run_id, "grace_seconds": grace_seconds},
    )
    status = control.read_status()
    if status["state"] != "cancel_requested":
        return
    canonical = storage_path / "views" / "runs" / control.run_id / "run.json"
    if canonical.is_file() and not canonical.is_symlink():
        try:
            Storage(storage_path).finalize_run_metadata(
                control.run_id,
                status="failed",
                update_latest_success=False,
            )
        except BaseException:
            pass
    try:
        control.commit_terminal(
            expected_revision=status["revision"],
            expected_claim_epoch=status["claim_epoch"],
            new_state="lost",
            error_payload=build_error_payload(
                control.run_id,
                code="orchestrator-hard-terminated",
                error=error,
                backend={"name": "local"},
            ),
            updates={
                "hard_termination_requested": True,
                "cancel_requested_at": None,
            },
        )
    except (RevisionConflictError, RuntimeError):
        return


def _schedule_hard_termination(
    control: LauncherRunControl,
    *,
    storage_path: Path,
    grace_seconds: float,
) -> None:
    threading.Thread(
        target=_hard_terminate_after_grace,
        kwargs={
            "control": control,
            "storage_path": storage_path,
            "grace_seconds": grace_seconds,
        },
        name=f"bioimageflow-hard-cancel-{control.run_id}",
        daemon=True,
    ).start()


class WorkflowRun:
    """Storage-backed submitted-run handle with no live process state."""

    def __init__(self, control: LauncherRunControl) -> None:
        self._control = control
        self.id = control.run_id
        self.control_dir = control.control_dir
        submission = control.read_submission()
        self._storage_path = Path(submission["storage_root"])
        self._launch = OrchestratorLaunchConfig.from_dict(submission["launch"])
        self.view_dir = self._storage_path / submission["canonical_view"]
        self._status = control.read_status()
        if (
            self._status["state"] == "cancel_requested"
            and self._launch.backend == "local"
            and self._launch.hard_cancel_after is not None
        ):
            _schedule_hard_termination(
                self._control,
                storage_path=self._storage_path,
                grace_seconds=self._launch.hard_cancel_after,
            )

    @classmethod
    def open(
        cls,
        storage_path: Path | str,
        run_id: str,
    ) -> "WorkflowRun":
        """Reconnect from exactly one explicit storage root and run ID."""
        return cls(LauncherRepository(storage_path).open(run_id))

    @property
    def status(self) -> str:
        """Return the last refreshed launcher state."""
        return str(self._status["state"])

    def refresh(self) -> None:
        """Refresh guarded state from durable launcher metadata."""
        self._status = self._control.read_status()

    def progress(
        self,
        *,
        after_sequence: int = 0,
    ) -> list[dict[str, Any]]:
        """Return persisted progress events after one global sequence."""
        if type(after_sequence) is not int or after_sequence < 0:
            raise ValueError("after_sequence must be a non-negative integer.")
        return [
            event
            for event in self._control.read_progress()
            if event["sequence"] > after_sequence
        ]

    def logs(self) -> str:
        """Return currently persisted orchestrator stdout and stderr."""
        sections: list[str] = []
        for label, relative in (
            ("stdout", "logs/orchestrator.out"),
            ("stderr", "logs/orchestrator.err"),
        ):
            path = self._control.confined_path(relative)
            if not path.exists():
                continue
            if path.is_symlink() or not path.is_file():
                raise RuntimeError(f"Orchestrator {label} log is unsafe.")
            sections.append(f"[{label}]\n{path.read_text(errors='replace')}")
        return "\n".join(sections)

    def cancel(self) -> None:
        """Request state-specific durable cancellation."""
        for _attempt in range(8):
            status = self._control.read_status()
            try:
                self._status = self._control.request_cancel(
                    expected_revision=status["revision"],
                    expected_claim_epoch=status["claim_epoch"],
                )
                if (
                    self._status["state"] == "cancel_requested"
                    and self._launch.backend == "local"
                    and self._launch.hard_cancel_after is not None
                ):
                    _schedule_hard_termination(
                        self._control,
                        storage_path=self._storage_path,
                        grace_seconds=self._launch.hard_cancel_after,
                    )
                return
            except RevisionConflictError:
                continue
        raise RuntimeError("Cancellation lost repeated concurrent status updates.")

    def result(self) -> Any:
        """Return the exact persisted public result or its stable state error."""
        self.refresh()
        state = self.status
        if state == "succeeded":
            return load_public_return(
                self.control_dir,
                self._storage_path,
                self.id,
            )
        if state == "failed":
            error = read_error(self._control)
            raise WorkflowRunFailedError(
                error["message"],
                error=error,
            )
        if state == "cancelled":
            raise WorkflowCancelledError(
                f"Submitted workflow run {self.id} was cancelled."
            )
        if state == "lost":
            details: dict[str, Any] = {}
            try:
                details = read_error(self._control)
            except Exception:
                pass
            raise WorkflowRunLostError(
                f"Submitted workflow run {self.id} was lost.",
                details=details,
            )
        raise WorkflowRunNotReadyError(
            f"Submitted workflow run {self.id} is {state!r}, not succeeded.",
            details={"run_id": self.id, "state": state},
        )
