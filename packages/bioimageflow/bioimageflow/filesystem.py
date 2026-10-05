"""Native atomic publication without replacing an occupied destination."""

from __future__ import annotations

import ctypes
import errno
import os
from pathlib import Path
import sys


def publish_no_replace(stage: Path, destination: Path) -> None:
    """Atomically publish without replacing any racing file or directory.

    Darwin RENAME_EXCL: apple-oss-distributions/xnu bsd/sys/stdio.h.
    Linux RENAME_NOREPLACE: linux include/uapi/linux/fs.h.
    Windows os.rename always refuses an existing destination (Python docs).
    Unsupported platforms/primitives fail rather than emulate check-then-rename.
    """
    if os.name == 'nt':
        os.rename(stage, destination)
        return
    libc = ctypes.CDLL(None, use_errno=True)
    if sys.platform == 'darwin':
        operation = getattr(libc, 'renamex_np', None)
        if operation is None:
            raise NotImplementedError('Atomic exclusive publication unavailable')
        operation.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        operation.restype = ctypes.c_int
        result = operation(os.fsencode(stage), os.fsencode(destination), 0x00000004)
    elif sys.platform.startswith('linux'):
        operation = getattr(libc, 'renameat2', None)
        if operation is None:
            raise NotImplementedError('Atomic exclusive publication unavailable')
        operation.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        operation.restype = ctypes.c_int
        result = operation(-100, os.fsencode(stage), -100, os.fsencode(destination), 1)
    else:
        raise NotImplementedError('Atomic exclusive publication unavailable')
    if result:
        error = ctypes.get_errno()
        if error in {errno.EEXIST, errno.ENOTEMPTY}:
            raise FileExistsError(error, os.strerror(error), str(destination))
        raise OSError(error, os.strerror(error), str(destination))
