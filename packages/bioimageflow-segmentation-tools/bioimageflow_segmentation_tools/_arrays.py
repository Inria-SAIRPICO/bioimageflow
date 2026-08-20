"""Array validation and label-image helpers shared by segmentation tools."""

from pathlib import Path
from typing import Any


def validate_image(image: Any, *, name: str, dimensions: tuple[int, ...]) -> Any:
    """Return *image* as an array after validating its dimensions and values."""
    import numpy as np

    array = np.asarray(image)
    if array.ndim not in dimensions:
        expected = " or ".join(f"{dimension}D" for dimension in dimensions)
        raise ValueError(f"{name} must be {expected}; got shape {array.shape}.")
    if not np.issubdtype(array.dtype, np.number) and array.dtype != np.bool_:
        raise ValueError(f"{name} must contain numeric values.")
    if np.issubdtype(array.dtype, np.inexact) and not np.isfinite(array).all():
        raise ValueError(f"{name} must contain only finite values.")
    return array


def validate_labels(
    labels: Any,
    *,
    name: str = "labels",
    dimensions: tuple[int, ...] = (2, 3),
    expected_shape: tuple[int, ...] | None = None,
) -> Any:
    """Return a non-negative integral label array after strict validation."""
    import numpy as np

    array = validate_image(labels, name=name, dimensions=dimensions)
    if expected_shape is not None and array.shape != expected_shape:
        raise ValueError(
            f"{name} must have shape {expected_shape}; got {array.shape}."
        )
    if not (
        np.issubdtype(array.dtype, np.integer)
        or np.issubdtype(array.dtype, np.floating)
        or array.dtype == np.bool_
    ):
        raise ValueError(f"{name} must contain integer label IDs.")
    if np.issubdtype(array.dtype, np.floating) and not np.equal(array, np.floor(array)).all():
        raise ValueError(f"{name} must contain integer label IDs.")
    if np.any(array < 0):
        raise ValueError(f"{name} must not contain negative label IDs.")
    maximum = int(array.max(initial=0))
    if maximum > np.iinfo(np.uint32).max:
        raise ValueError(f"{name} contains a label ID that does not fit in uint32.")
    return array.astype(np.uint32, copy=False)


def object_count(labels: Any) -> int:
    """Count distinct positive labels without assuming sequential IDs."""
    import numpy as np

    values = np.unique(np.asarray(labels))
    return int(np.count_nonzero(values > 0))


def relabel_sequential(labels: Any, *, min_size: int = 0) -> tuple[Any, int]:
    """Filter small labels and map retained positive IDs to ``1..N``."""
    import numpy as np

    source = validate_labels(labels)
    values, inverse, counts = np.unique(source, return_inverse=True, return_counts=True)
    retained = (values > 0) & (counts >= min_size)
    mapped_values = np.zeros(values.shape, dtype=np.uint32)
    mapped_values[retained] = np.arange(1, int(retained.sum()) + 1, dtype=np.uint32)
    output = mapped_values[inverse].reshape(source.shape)
    return output, int(retained.sum())


def write_labels(path: Path, labels: Any) -> None:
    """Write a validated label image with an unambiguous TIFF photometric mode."""
    import imageio.v3 as iio
    import numpy as np

    array = validate_labels(labels).astype(np.uint32, copy=False)
    path.parent.mkdir(parents=True, exist_ok=True)
    kwargs = {"photometric": "minisblack"} if array.ndim >= 3 else {}
    iio.imwrite(str(path), array, **kwargs)


def finite_float(value: Any, *, name: str) -> float:
    """Validate and return a finite floating-point parameter."""
    import math

    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{name} must be finite.")
    return result


def integer_parameter(value: Any, *, name: str, minimum: int = 0) -> int:
    """Validate an integer parameter without silently truncating floats."""
    import numbers

    if isinstance(value, bool) or not isinstance(value, numbers.Integral):
        raise ValueError(f"{name} must be an integer greater than or equal to {minimum}.")
    result = int(value)
    if result < minimum:
        raise ValueError(f"{name} must be greater than or equal to {minimum}.")
    return result
