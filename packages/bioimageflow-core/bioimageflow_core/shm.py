"""Shared memory helpers that expose NumPy array views."""

from collections.abc import Generator
from contextlib import contextmanager
from typing import Any, Optional

from bioimageflow_core.types import SharedArray
from bioimageflow_core.shared_memory import get_shared_memory_context
from bioimageflow_core._shared_storage import dtype as _shared_memory_dtype


@contextmanager
def create_shared_output(data: Any, name: Optional[str] = None) -> Generator[SharedArray, None, None]:
    """
    Copy data into the explicitly active controller-owned allocation namespace.
    Lexical exit does not release the reference or its backing storage.
    """
    import numpy as np
    arr = np.asarray(data)
    _shared_memory_dtype(arr.dtype)
    yield get_shared_memory_context().create(arr, name=name)


@contextmanager
def open_shared_array(ref: SharedArray, *, writable: Optional[bool] = None) -> Generator[Any, None, None]:
    """
    Map admitted backing without copying; accepted snapshots are read-only.
    Explicit writable access is available only for unpublished producer allocations.
    Live arrays and derived views retain the mapping beyond lexical exit.
    """
    _shared_memory_dtype(ref.dtype)
    context = ref.bound_owner or get_shared_memory_context()
    yield context.open(ref, writable=writable)
