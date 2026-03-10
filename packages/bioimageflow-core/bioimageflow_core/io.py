"""I/O dispatch — zero declared dependencies. Uses numpy at runtime."""

from collections.abc import Callable, Generator
from contextlib import contextmanager
from multiprocessing.shared_memory import SharedMemory
from pathlib import Path
from typing import Any


@contextmanager
def load_image(source: Any, *, file_reader: Callable[[Path], Any]) -> Generator[Any, None, None]:
    """
    Dispatch between file and shared memory sources.
    - SharedArray: attaches to shared memory, yields numpy view.
    - Path or str: delegates to file_reader, yields result.
    """
    from bioimageflow_core.types import SharedArray

    if isinstance(source, SharedArray):
        import numpy as np
        shm = SharedMemory(name=source.name)
        try:
            arr = np.ndarray(source.shape, dtype=source.dtype, buffer=shm.buf)
            yield arr
        finally:
            shm.close()
    else:
        yield file_reader(Path(source))


def save_image(destination: str | Path, data: Any, *, file_writer: Callable[[Path, Any], None]) -> None:
    """Save image data to disk using the provided writer."""
    file_writer(Path(destination), data)
