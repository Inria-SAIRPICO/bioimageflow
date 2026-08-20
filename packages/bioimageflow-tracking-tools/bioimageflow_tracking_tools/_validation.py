"""Shared validation helpers for tracking tables and label images."""

from typing import Any


def require_columns(df: Any, columns: set[str], tool_name: str) -> None:
    """Raise a useful error when a dataframe is missing required columns."""
    missing = sorted(columns - set(df.columns))
    if missing:
        formatted = ", ".join(repr(column) for column in missing)
        raise ValueError(
            f"{tool_name} input table is missing required column(s): {formatted}."
        )


def finite_float(value: Any, name: str) -> float:
    """Return a finite floating-point value."""
    import numpy as np

    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a finite number.") from exc
    if not np.isfinite(result):
        raise ValueError(f"{name} must be a finite number.")
    return result


def integral_value(
    value: Any,
    name: str,
    *,
    minimum: int = 0,
    maximum: int | None = None,
) -> int:
    """Return a finite integer in the requested inclusive range."""
    number = finite_float(value, name)
    if not number.is_integer():
        raise ValueError(f"{name} must be an integer.")
    result = int(number)
    if result < minimum:
        qualifier = "positive" if minimum == 1 else f">= {minimum}"
        raise ValueError(f"{name} must be {qualifier}.")
    if maximum is not None and result > maximum:
        raise ValueError(f"{name} must be <= {maximum}.")
    return result


def validate_tracking_columns(
    df: Any,
    *,
    tool_name: str,
    require_coordinates: bool = True,
    require_label: bool = False,
    require_area: bool = False,
) -> Any:
    """Return a copy with canonical validated numeric tracking columns."""
    import numpy as np
    import pandas as pd

    required = {"track_id", "frame"}
    if require_coordinates:
        required.update({"y", "x"})
    if require_label:
        required.add("label")
    if require_area:
        required.add("area")
    require_columns(df, required, tool_name)

    result = df.copy()
    for column in required:
        try:
            values = np.asarray(
                pd.to_numeric(result[column], errors="raise"), dtype=np.float64
            )
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{tool_name} column {column!r} must be numeric.") from exc
        if not np.isfinite(values).all():
            raise ValueError(
                f"{tool_name} column {column!r} must contain only finite values."
            )
        if column in {"track_id", "frame", "label"}:
            if not np.equal(values, np.floor(values)).all():
                raise ValueError(
                    f"{tool_name} column {column!r} must contain only integers."
                )
            minimum = 0 if column == "frame" else 1
            if (values < minimum).any():
                qualifier = "non-negative" if minimum == 0 else "positive"
                raise ValueError(
                    f"{tool_name} column {column!r} must contain only {qualifier} integers."
                )
            result[column] = values.astype(np.int64)
        else:
            if column == "area" and (values < 0).any():
                raise ValueError(f"{tool_name} column 'area' must be non-negative.")
            result[column] = values
    return result


def validate_label_image(labels: Any, tool_name: str) -> None:
    """Require a two- or three-dimensional non-negative integer label raster."""
    import numpy as np

    if labels.ndim not in {2, 3}:
        raise ValueError(f"{tool_name} expects a 2D label image or TYX stack.")
    if not np.issubdtype(labels.dtype, np.integer):
        raise ValueError(f"{tool_name} expects an integer label image.")
    if np.issubdtype(labels.dtype, np.signedinteger) and (labels < 0).any():
        raise ValueError(f"{tool_name} label image must not contain negative labels.")
