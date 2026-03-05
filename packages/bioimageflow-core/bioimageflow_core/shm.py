"""Shared memory helpers — zero declared dependencies. Uses numpy at runtime."""

import uuid
from collections.abc import Generator
from contextlib import contextmanager
from typing import Any

from bioimageflow_core.types import SharedArray


@contextmanager
def create_shared_output(data: Any, name: str | None = None) -> Generator[SharedArray, None, None]:
    """
    Create a shared memory segment, copy data into it, and yield a SharedArray.
    Closes the local handle on exit but does NOT unlink (data persists).
    """
    import numpy as np
    from multiprocessing.shared_memory import SharedMemory

    arr = np.asarray(data)
    if name is None:
        name = f"bif_{uuid.uuid4().hex[:16]}"

    shm = SharedMemory(name=name, create=True, size=arr.nbytes)
    try:
        shared_arr = np.ndarray(arr.shape, dtype=arr.dtype, buffer=shm.buf)
        shared_arr[:] = arr[:]
        ref = SharedArray(name=shm.name, shape=arr.shape, dtype=str(arr.dtype))
        yield ref
    finally:
        shm.close()


@contextmanager
def open_shared_array(ref: SharedArray) -> Generator[Any, None, None]:
    """
    Attach to an existing shared memory segment.
    Yields a zero-copy numpy array. Closes handle on exit.
    """
    import numpy as np
    from multiprocessing.shared_memory import SharedMemory

    shm = SharedMemory(name=ref.name)
    try:
        arr = np.ndarray(ref.shape, dtype=ref.dtype, buffer=shm.buf)
        yield arr
    finally:
        shm.close()
