"""Neutral execution outcome values shared by scheduling and persistence."""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol


class WorkflowCancelledError(Exception):
    """Raised when a workflow execution is cancelled via ``Workflow.cancel()``."""


class CleanupRecorder(Protocol):
    """Persistence reports detached secondary failures to its execution owner."""

    def _record_cleanup_failure(
        self, phase: str, error: BaseException, retry: Callable[[], None] | None = None,
    ) -> None: ...
