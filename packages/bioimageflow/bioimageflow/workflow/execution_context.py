"""Runtime-only state for one public workflow execution."""

from __future__ import annotations

import re
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path


_RUN_ID_RE = re.compile(r"^run_[0-9a-f]{32}$")
_INVOCATION_ID_RE = re.compile(r"^inv_[0-9a-f]{32}$")


@dataclass(frozen=True)
class ExecutionProviderOutcome:
    """Exact runtime identity for one successfully executed real provider."""

    node_key: str
    result_key: str | None
    record_id: str | None
    transient_invocation_id: str | None
    path_columns: tuple[str, ...]
    owned_path_columns: tuple[str, ...]
    shared_array_columns: tuple[str, ...]

    @property
    def storage_kind(self) -> str:
        """Return the durable location kind available for this provider."""
        if self.result_key is not None:
            return "record"
        if self.transient_invocation_id is not None:
            return "transient"
        return "memory"


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
        self._execution_outcomes: dict[str, ExecutionProviderOutcome] = {}
        self._launcher_storage_path: Path | None = None

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

    @property
    def execution_outcomes(self) -> tuple[ExecutionProviderOutcome, ...]:
        """Return an immutable, deterministically ordered provider snapshot."""
        with self._lock:
            return tuple(
                self._execution_outcomes[node_key]
                for node_key in sorted(self._execution_outcomes)
            )

    def request_cancel(self) -> None:
        """Request cancellation without affecting any other execution."""
        self._cancel_event.set()

    def _authorize_launcher_reservation(self, storage_path: str | Path) -> None:
        """Authorize canonical creation for an already reserved submitted run."""
        normalized = Path(storage_path).expanduser().resolve(strict=False)
        with self._lock:
            if self._state != "new":
                raise RuntimeError(
                    "Launcher reservation must be authorized before execution."
                )
            self._launcher_storage_path = normalized

    def _uses_launcher_reservation(self, storage_path: str | Path) -> bool:
        normalized = Path(storage_path).expanduser().resolve(strict=False)
        with self._lock:
            return self._launcher_storage_path == normalized

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

    def _record_provider_outcome(
        self,
        *,
        node_key: str,
        result_key: str | None,
        record_id: str | None,
        transient_invocation_id: str | None,
        path_columns: set[str],
        owned_path_columns: set[str],
        shared_array_columns: set[str],
    ) -> None:
        """Record exact provider identity without exposing engine-local state."""
        if (
            not isinstance(node_key, str)
            or not node_key
            or any(part in {"", ".", ".."} for part in node_key.split("/"))
        ):
            raise ValueError("Provider node_key must be a safe scoped node path.")
        if (result_key is None) != (record_id is None):
            raise ValueError(
                "Provider result_key and record_id must be supplied together."
            )
        if result_key is not None and transient_invocation_id is not None:
            raise ValueError(
                "A provider outcome cannot be both record-backed and transient."
            )
        if (
            transient_invocation_id is not None
            and _INVOCATION_ID_RE.fullmatch(transient_invocation_id) is None
        ):
            raise ValueError("Provider transient invocation ID is invalid.")

        def normalized_columns(values: set[str], *, label: str) -> tuple[str, ...]:
            if not isinstance(values, set) or any(
                not isinstance(value, str) or not value for value in values
            ):
                raise TypeError(f"Provider {label} must be a set of non-empty strings.")
            return tuple(sorted(values))

        outcome = ExecutionProviderOutcome(
            node_key=node_key,
            result_key=result_key,
            record_id=record_id,
            transient_invocation_id=transient_invocation_id,
            path_columns=normalized_columns(path_columns, label="path_columns"),
            owned_path_columns=normalized_columns(
                owned_path_columns,
                label="owned_path_columns",
            ),
            shared_array_columns=normalized_columns(
                shared_array_columns,
                label="shared_array_columns",
            ),
        )
        with self._lock:
            if self._state != "running":
                raise RuntimeError(
                    "Provider outcomes can be recorded only during active execution."
                )
            existing = self._execution_outcomes.get(node_key)
            if existing is not None and existing != outcome:
                raise RuntimeError(
                    f"Provider outcome for {node_key!r} was recorded inconsistently."
                )
            self._execution_outcomes[node_key] = outcome

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
