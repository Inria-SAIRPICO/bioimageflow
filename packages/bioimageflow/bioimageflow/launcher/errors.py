"""Stable public errors for submitted workflow runs."""

from __future__ import annotations

from typing import Any


class LauncherError(RuntimeError):
    """Base class for launcher failures with a stable machine-readable code."""

    code = "launcher-error"

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.details = dict(details or {})


class BackendNotSupportedError(LauncherError):
    """Raised when a launcher backend has no installed adapter."""

    code = "backend-not-supported"


class WorkflowRunFailedError(LauncherError):
    """Raised when a submitted workflow terminated as failed."""

    code = "workflow-run-failed"

    def __init__(self, message: str, *, error: dict[str, Any]) -> None:
        super().__init__(message, details=error)
        self.error = dict(error)


class WorkflowRunLostError(LauncherError):
    """Raised when an orchestrator disappeared without a safe terminal outcome."""

    code = "workflow-run-lost"


class WorkflowRunNotReadyError(LauncherError):
    """Raised when a submitted result is requested before success."""

    code = "workflow-run-not-ready"


class WorkflowRunResultUnavailableError(LauncherError):
    """Raised when a successful historical return can no longer be reconstructed."""

    code = "workflow-run-result-unavailable"


class LauncherProtocolError(LauncherError):
    """Raised when persisted launcher control state is malformed or unsafe."""

    code = "launcher-protocol-error"


class LauncherStateConflictError(LauncherError):
    """Raised when a guarded revision, claim, or transition loses a race."""

    code = "launcher-state-conflict"
