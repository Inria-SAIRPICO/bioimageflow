"""Lease, cancellation, and progress monitoring for an orchestrator."""

from __future__ import annotations

import os
import socket
import threading
import uuid
from collections.abc import Mapping
from typing import Any

from bioimageflow.events import ProgressEvent
from bioimageflow.workflow import WorkflowExecutionContext

from .control import LauncherRunControl
from .errors import LauncherProtocolError


_PUBLIC_PROGRESS_SCHEMA = "bioimageflow.progress_event.v1"
_BACKEND_PROGRESS_SCHEMA = "bioimageflow.launcher.backend_event.v1"


class ClaimHeartbeat:
    """Renew one execution claim and cancel work after claim loss."""

    def __init__(
        self,
        control: LauncherRunControl,
        *,
        claim: Mapping[str, Any],
        workflow: Any | None,
        lease_seconds: float,
        poll_seconds: float,
    ) -> None:
        self._control = control
        self._claim = dict(claim)
        self._workflow = workflow
        self._lease_seconds = lease_seconds
        self._interval = min(poll_seconds, lease_seconds / 3)
        self._stop = threading.Event()
        self._failure: BaseException | None = None
        self._thread = threading.Thread(
            target=self._run,
            name=f"bioimageflow-heartbeat-{control.run_id}",
            daemon=True,
        )

    def start(self) -> None:
        self._thread.start()

    def attach_workflow(self, workflow: Any) -> None:
        self._workflow = workflow

    def stop(self) -> None:
        self._stop.set()
        self._thread.join()

    def raise_if_failed(self) -> None:
        if self._failure is not None:
            raise LauncherProtocolError(
                "The orchestrator lost its execution claim.",
                details={"run_id": self._control.run_id},
            ) from self._failure

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                self._claim = self._control.heartbeat_claim(
                    expected_epoch=self._claim["epoch"],
                    expected_nonce=self._claim["nonce"],
                    lease_seconds=self._lease_seconds,
                )
            except BaseException as exc:
                self._failure = exc
                if self._workflow is not None:
                    self._workflow.cancel()
                return


class CancellationWatcher:
    """Project authoritative durable cancellation into one live context."""

    def __init__(
        self,
        control: LauncherRunControl,
        workflow: Any,
        context: WorkflowExecutionContext,
        *,
        poll_seconds: float,
    ) -> None:
        self._control = control
        self._workflow = workflow
        self._context = context
        self._poll_seconds = poll_seconds
        self._stop = threading.Event()
        self._failure: BaseException | None = None
        self._thread = threading.Thread(
            target=self._run,
            name=f"bioimageflow-cancel-{control.run_id}",
            daemon=True,
        )

    def start(self) -> None:
        self._check()
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join()

    def raise_if_failed(self) -> None:
        if self._failure is not None:
            raise LauncherProtocolError(
                "The orchestrator cancellation watcher failed.",
                details={"run_id": self._control.run_id},
            ) from self._failure

    def _run(self) -> None:
        while not self._stop.wait(self._poll_seconds):
            if not self._check():
                return

    def _check(self) -> bool:
        try:
            status = self._control.read_status()
            if self._control.cancellation_marker_exists():
                status = self._control.read_status()
        except BaseException as exc:
            self._failure = exc
            self._context.request_cancel()
            self._workflow.cancel()
            return False
        if status["state"] == "cancel_requested":
            self._context.request_cancel()
            self._workflow.cancel()
        return status["state"] not in {
            "finalizing",
            "succeeded",
            "failed",
            "cancelled",
            "lost",
        }


def owner_identity() -> str:
    """Return a unique diagnostic identity for this orchestrator process."""
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex}"


def public_progress_payload(event: ProgressEvent) -> dict[str, Any]:
    """Convert one public event to its exact launcher payload."""
    return {
        "schema": _PUBLIC_PROGRESS_SCHEMA,
        "node_name": event.node_name,
        "status": event.status,
        "row": event.row,
        "total_rows": event.total_rows,
        "message": event.message,
        "current": event.current,
        "maximum": event.maximum,
        "timestamp": event.timestamp,
        "result_key": event.result_key,
        "record_id": event.record_id,
    }


def append_backend_event(
    control: LauncherRunControl,
    event: str,
    *,
    claim_epoch: int,
    claim_nonce: str,
    allow_terminal_claim: bool = False,
    **details: Any,
) -> None:
    """Append one backend event while the supplied claim still owns the run."""
    control.append_progress(
        kind="backend",
        payload={
            "schema": _BACKEND_PROGRESS_SCHEMA,
            "event": event,
            **details,
        },
        expected_claim_epoch=claim_epoch,
        expected_claim_nonce=claim_nonce,
        allow_terminal_claim=allow_terminal_claim,
    )
