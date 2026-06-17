"""I/O dispatch helpers with pluggable file readers and NumPy shared-memory views."""

from collections.abc import Callable, Generator
from contextlib import contextmanager
from multiprocessing.shared_memory import SharedMemory
from pathlib import Path
from typing import Any, Union

from bioimageflow_core.types import SharedArray


@contextmanager
def load_image(source: Any, *, file_reader: Callable[[Path], Any]) -> Generator[Any, None, None]:
    """
    Dispatch between file and shared memory sources.
    - SharedArray: attaches to shared memory, yields numpy view.
    - Path or str: delegates to file_reader, yields result.
    """
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


def save_image(destination: Union[str, Path], data: Any, *, file_writer: Callable[[Path, Any], None]) -> None:
    """Save image data to disk using the provided writer."""
    file_writer(Path(destination), data)
