"""Shared validation for spot identifiers, coordinates, and raster inputs."""

from typing import Any


def finite_float(value: Any, name: str) -> float:
    """Return a finite float or raise a field-specific validation error."""
    import numpy as np

    try:
        result = float(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{name} must be a finite number.") from error
    if not np.isfinite(result):
        raise ValueError(f"{name} must be a finite number.")
    return result


def integral_value(
    value: Any,
    name: str,
    *,
    minimum: int,
    maximum: int | None = None,
) -> int:
    """Return an exactly integral numeric value within the requested range."""
    numeric = finite_float(value, name)
    if not numeric.is_integer():
        raise ValueError(f"{name} must be an integer.")
    result = int(numeric)
    if result < minimum:
        qualifier = "positive" if minimum == 1 else f">= {minimum}"
        raise ValueError(f"{name} must be {qualifier}.")
    if maximum is not None and result > maximum:
        raise ValueError(f"{name} must be <= {maximum}.")
    return result


def positive_uint32_id(value: Any, name: str = "spot_id") -> int:
    """Validate a positive label identifier, reserving zero for background."""
    import numpy as np

    numeric = finite_float(value, name)
    if not numeric.is_integer() or numeric <= 0:
        raise ValueError(
            f"{name} must be a positive integer; 0 is reserved for background."
        )
    maximum = int(np.iinfo(np.uint32).max)
    if numeric > maximum:
        raise ValueError(f"{name} must be <= {maximum}.")
    return int(numeric)


def nearest_pixel(value: Any, name: str) -> int:
    """Round a finite coordinate to its nearest pixel, with half values rounded up."""
    import numpy as np

    return int(np.floor(finite_float(value, name) + 0.5))


def pixel_coordinate(
    y: Any,
    x: Any,
    shape: tuple[int, int],
) -> tuple[float, float, int, int]:
    """Validate a coordinate and return both continuous and nearest-pixel values."""
    if y is None or y == "":
        raise ValueError("Spot table row is missing required column 'y'.")
    if x is None or x == "":
        raise ValueError("Spot table row is missing required column 'x'.")
    y_float = finite_float(y, "y")
    x_float = finite_float(x, "x")
    y_pixel = nearest_pixel(y_float, "y")
    x_pixel = nearest_pixel(x_float, "x")
    if not (0 <= y_pixel < shape[0] and 0 <= x_pixel < shape[1]):
        raise ValueError(
            f"Spot coordinate ({y_float}, {x_float}) is outside image bounds {shape}."
        )
    return y_float, x_float, y_pixel, x_pixel


def planar_array(array: Any, name: str) -> Any:
    """Require a scalar two-dimensional raster."""
    if array.ndim != 2:
        raise ValueError(f"{name} must be a 2D scalar image; got shape {array.shape}.")
    return array


def label_array(array: Any, name: str = "label_image") -> Any:
    """Require a finite, non-negative, integral 2D label raster."""
    import numpy as np

    planar_array(array, name)
    if not np.issubdtype(array.dtype, np.number):
        raise ValueError(f"{name} must contain numeric labels.")
    numeric = np.asarray(array)
    if not np.all(np.isfinite(numeric)):
        raise ValueError(f"{name} must contain only finite labels.")
    if np.any(numeric < 0) or np.any(numeric != np.floor(numeric)):
        raise ValueError(f"{name} labels must be non-negative integers.")
    return numeric
