"""Backend-neutral cross-process run allocation."""

from __future__ import annotations

import errno
import os
import stat
import sys
from pathlib import Path

if sys.platform == "win32":
    import msvcrt
else:
    import fcntl


class RunAllocationLock:
    """An exclusive filesystem lock shared by every run namespace."""

    def __init__(self, storage_path: Path) -> None:
        self.path = storage_path / "launcher" / "v1" / ".allocation.guard"
        self._descriptor: int | None = None

    def __enter__(self) -> "RunAllocationLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.is_symlink():
            raise RuntimeError("Run allocation guard must not be a symlink.")
        flags = os.O_RDWR | os.O_CREAT
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(self.path, flags, 0o600)
        except OSError as error:
            if error.errno == errno.ELOOP:
                raise RuntimeError(
                    "Run allocation guard must not be a symlink."
                ) from error
            raise
        if not stat.S_ISREG(os.fstat(descriptor).st_mode) or self.path.is_symlink():
            os.close(descriptor)
            raise RuntimeError(
                "Run allocation guard must be a regular non-symlink file."
            )
        try:
            if sys.platform == "win32":
                if os.fstat(descriptor).st_size == 0:
                    os.write(descriptor, b"\0")
                    os.fsync(descriptor)
                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_LOCK, 1)
            else:
                fcntl.flock(descriptor, fcntl.LOCK_EX)
        except BaseException:
            os.close(descriptor)
            raise
        self._descriptor = descriptor
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        descriptor = self._descriptor
        self._descriptor = None
        if descriptor is None:
            return
        try:
            if sys.platform == "win32":
                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)
