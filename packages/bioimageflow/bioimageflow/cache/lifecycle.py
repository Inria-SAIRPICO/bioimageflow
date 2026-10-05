"""Owned reusable-attempt failure persistence shared by local tool families."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import logging

from bioimageflow.storage import CacheCorruptionError, Storage

from bioimageflow.execution_state import CleanupRecorder, WorkflowCancelledError


@dataclass
class AttemptFailure:
    """Detached exact directory authority, never a captured scientific traceback."""

    storage_path: Path
    result_key: str
    attempt_id: str
    identity: tuple[int, int]
    status: str = "failed"
    error_type: str | None = None

    @classmethod
    def capture(cls, storage: Storage, result_key: str, attempt_id: str) -> AttemptFailure:
        directory = storage.result_dir(result_key) / "attempts" / attempt_id
        stat = directory.stat(follow_symlinks=False)
        if directory.is_symlink():
            raise CacheCorruptionError("Owned cache attempt directory is a symbolic link.")
        return cls(storage.storage_path, result_key, attempt_id, (stat.st_dev, stat.st_ino))

    def finish(self, primary: BaseException, context: CleanupRecorder | None) -> None:
        cancelled = isinstance(primary, WorkflowCancelledError)
        self.status = "cancelled" if cancelled else "failed"
        self.error_type = None if cancelled else type(primary).__name__
        try:
            self.retry()
        except BaseException as secondary:
            if context is not None:
                context._record_cleanup_failure("cache-attempt-finalization", secondary, self.retry)
            else:
                logging.getLogger("bioimageflow").exception("Cache attempt finalization remains pending")

    def retry(self) -> None:
        storage = Storage(self.storage_path)
        directory = storage.result_dir(self.result_key) / "attempts" / self.attempt_id
        stat = directory.stat(follow_symlinks=False)
        if directory.is_symlink() or (stat.st_dev, stat.st_ino) != self.identity:
            raise CacheCorruptionError("Owned cache attempt directory identity changed.")
        if (directory / "attempt.json").is_symlink():
            raise CacheCorruptionError("Owned cache attempt metadata is a symbolic link.")
        storage.finish_cache_attempt(
            self.result_key, self.attempt_id, status=self.status, error_type=self.error_type,
        )
