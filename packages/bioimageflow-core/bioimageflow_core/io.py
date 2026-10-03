"""I/O dispatch helpers with pluggable file readers and NumPy shared-memory views."""

from collections.abc import Callable, Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Union

from bioimageflow_core.types import SharedArray
from bioimageflow_core.shm import open_shared_array


@contextmanager
def load_image(source: Any, *, file_reader: Callable[[Path], Any]) -> Generator[Any, None, None]:
    """
    Dispatch between file and shared memory sources.
    - SharedArray: maps an admitted numeric backing, yields a retained NumPy view.
    - Path or str: delegates to file_reader, yields result.
    """
    if isinstance(source, SharedArray):
        with open_shared_array(source) as arr:
            yield arr
    else:
        yield file_reader(Path(source))


def save_image(destination: Union[str, Path], data: Any, *, file_writer: Callable[[Path, Any], None]) -> None:
    """Save image data to disk using the provided writer."""
    file_writer(Path(destination), data)
