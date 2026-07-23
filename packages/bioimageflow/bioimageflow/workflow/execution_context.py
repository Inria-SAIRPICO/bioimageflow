"""Runtime-only state for one public workflow execution."""

from __future__ import annotations

import re
import threading
import uuid
from collections.abc import Callable


_RUN_ID_RE = re.compile(r"^run_[0-9a-f]{32}$")


class WorkflowExecutionContext:
    """Cancellation, run identity, and finalization for one root execution."""

    def __init__(
        self,
        run_id: str | None = None,
        *,
        defer_success_finalization: bool = False,
    ) -> None:
        if run_id is not None and _RUN_ID_RE.fullmatch(run_id) is None:
            raise ValueError(
                "run_id must use the form 'run_' followed by 32 lowercase "
                "hexadecimal characters."
            )
        if not isinstance(defer_success_finalization, bool):
            raise TypeError("defer_success_finalization must be a bool.")
        self.run_id = run_id
        self.defer_success_finalization = defer_success_finalization
        self._cancel_event = threading.Event()
        self._lock = threading.RLock()
        self._binding: object | None = None
        self._success_callback: Callable[[], None] | None = None
        self._failure_callback: Callable[[BaseException], None] | None = None
        self._state = "new"

    @property
    def cancel_requested(self) -> bool:
        """Whether cancellation has been requested for this execution."""
        return self._cancel_event.is_set()

    @property
    def terminal_status(self) -> str | None:
        """Return the terminal status, or ``None`` while not finalized."""
        with self._lock:
            if self._state in {"succeeded", "failed"}:
                return self._state
            return None

    def request_cancel(self) -> None:
        """Request cancellation without affecting any other execution."""
        self._cancel_event.set()

    def finalize_success(self) -> None:
        """Finalize a successfully computed run."""
        with self._lock:
            if self._state != "awaiting_success":
                raise RuntimeError(
                    "Success can be finalized only after a deferred compute "
                    "returns successfully."
                )
        self._finalize("succeeded", None)

    def finalize_failure(self, error: BaseException) -> None:
        """Finalize a run as failed after return persistence fails."""
        if not isinstance(error, BaseException):
            raise TypeError("error must be an exception.")
        self._finalize("failed", error)

    def _ensure_run_id(self) -> str:
        with self._lock:
            if self.run_id is None:
                self.run_id = f"run_{uuid.uuid4().hex}"
            return self.run_id

    def _bind(
        self,
        binding: object,
        *,
        on_success: Callable[[], None],
        on_failure: Callable[[BaseException], None],
    ) -> str:
        with self._lock:
            if self._state != "new" or self._binding is not None:
                raise RuntimeError(
                    "WorkflowExecutionContext is already bound or finalized."
                )
            self._binding = binding
            self._success_callback = on_success
            self._failure_callback = on_failure
            self._state = "running"
            return self._ensure_run_id()

    def _execution_succeeded(self) -> None:
        with self._lock:
            if self._state != "running":
                raise RuntimeError(
                    "WorkflowExecutionContext is not running this execution."
                )
            if self.defer_success_finalization:
                self._state = "awaiting_success"
                return
        self._finalize("succeeded", None)

    def _execution_failed(self, error: BaseException) -> None:
        self._finalize("failed", error)

    def _finalize(
        self,
        status: str,
        error: BaseException | None,
    ) -> None:
        with self._lock:
            allowed = (
                {"running", "awaiting_success"}
                if status == "failed"
                else {"running", "awaiting_success"}
            )
            if self._state not in allowed or self._binding is None:
                raise RuntimeError(
                    "WorkflowExecutionContext cannot be finalized in its "
                    f"current state '{self._state}'."
                )
            callback: Callable[..., None] | None
            callback = (
                self._success_callback
                if status == "succeeded"
                else self._failure_callback
            )
            self._state = "finalizing"

        try:
            if status == "succeeded":
                assert callback is not None
                callback()
            else:
                assert callback is not None
                assert error is not None
                callback(error)
        except BaseException:
            with self._lock:
                self._state = "awaiting_success" if status == "succeeded" else "running"
            raise
        else:
            with self._lock:
                self._state = status
