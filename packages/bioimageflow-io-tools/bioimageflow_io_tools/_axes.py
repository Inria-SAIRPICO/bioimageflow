"""Axis validation and array selection shared by I/O tools."""

from collections.abc import Iterable
from typing import Any


SUPPORTED_AXES = frozenset("TCZYXS")


def normalize_axis_order(axis_order: str, shape: tuple[int, ...]) -> str:
    """Validate and normalize an axis order for *shape*."""
    normalized = str(axis_order).strip().upper()
    if not normalized:
        raise ValueError("Axis order must not be empty.")
    unknown = sorted(set(normalized) - SUPPORTED_AXES)
    if unknown:
        raise ValueError(
            f"Axis order {axis_order!r} contains unknown axes: {''.join(unknown)}."
        )
    if len(normalized) != len(shape):
        raise ValueError(
            f"Axis order {axis_order!r} has {len(normalized)} axes, "
            f"but image has {len(shape)} dimensions."
        )
    if len(set(normalized)) != len(normalized):
        raise ValueError(f"Axis order {axis_order!r} contains duplicate axes.")
    if "S" in normalized:
        sample_axis = normalized.index("S")
        if sample_axis != len(normalized) - 1:
            raise ValueError("Sample axis 'S' must be the final axis.")
        if shape[sample_axis] not in {3, 4}:
            raise ValueError("Sample axis 'S' must have size 3 (RGB) or 4 (RGBA).")
    return normalized


def normalize_requested_axes(axes: str) -> str:
    """Validate a set-like axis string used by layout requirements."""
    normalized = str(axes or "").strip().upper()
    unknown = sorted(set(normalized) - SUPPORTED_AXES)
    if unknown:
        raise ValueError(
            f"Required axes {axes!r} contains unknown axes: {''.join(unknown)}."
        )
    if len(set(normalized)) != len(normalized):
        raise ValueError(f"Required axes {axes!r} contains duplicate axes.")
    return normalized


def select_indices(
    data: Any,
    *,
    axis_order: str,
    selections: dict[str, int | None],
) -> tuple[Any, str]:
    """Select validated zero-based indices and return data plus remaining axes."""
    normalized = normalize_axis_order(axis_order, tuple(data.shape))
    selectors: list[int | slice] = [slice(None)] * data.ndim
    selected_axes: set[str] = set()
    for axis, index in selections.items():
        if index is None:
            continue
        if axis not in normalized:
            raise ValueError(
                f"Cannot select {axis}: axis order {axis_order!r} has no {axis} axis."
            )
        axis_index = normalized.index(axis)
        _validate_index(axis, index, int(data.shape[axis_index]))
        selectors[axis_index] = index
        selected_axes.add(axis)
    remaining_axes = "".join(axis for axis in normalized if axis not in selected_axes)
    return data[tuple(selectors)], remaining_axes


def select_axis_range(
    data: Any,
    *,
    axis_order: str,
    axis: str,
    start: int,
    stop: int | None,
) -> tuple[Any, str]:
    """Select a non-empty, fully in-bounds half-open range on one axis."""
    normalized = normalize_axis_order(axis_order, tuple(data.shape))
    if axis not in normalized:
        raise ValueError(
            f"Cannot select {axis}: axis order {axis_order!r} has no {axis} axis."
        )
    axis_index = normalized.index(axis)
    axis_size = int(data.shape[axis_index])
    _validate_bound(axis, "range start", start)
    if stop is not None:
        _validate_bound(axis, "range stop", stop)
    normalized_stop = axis_size if stop is None else stop
    if start < 0:
        raise IndexError(f"{axis} range start {start} must be non-negative.")
    if normalized_stop < 0:
        raise IndexError(f"{axis} range stop {normalized_stop} must be non-negative.")
    if start >= axis_size:
        raise IndexError(
            f"{axis} range start {start} is out of range for axis size {axis_size}."
        )
    if normalized_stop > axis_size:
        raise IndexError(
            f"{axis} range stop {normalized_stop} is out of range for axis size {axis_size}."
        )
    if normalized_stop <= start:
        raise ValueError(f"{axis} range must be non-empty: stop must be greater than start.")
    selectors: list[int | slice] = [slice(None)] * data.ndim
    selectors[axis_index] = slice(start, normalized_stop)
    return data[tuple(selectors)], normalized


def validate_unbound_axis_order(axis_order: str) -> str:
    """Validate axes before an array shape is available."""
    normalized = str(axis_order).strip().upper()
    if not normalized:
        raise ValueError("Axis order must not be empty.")
    unknown = sorted(set(normalized) - SUPPORTED_AXES)
    if unknown:
        raise ValueError(
            f"Axis order {axis_order!r} contains unknown axes: {''.join(unknown)}."
        )
    if len(set(normalized)) != len(normalized):
        raise ValueError(f"Axis order {axis_order!r} contains duplicate axes.")
    return normalized


def remaining_axis_order(axis_order: str, selected_axes: Iterable[str]) -> str:
    """Return *axis_order* without explicitly selected axes."""
    selected = set(selected_axes)
    remaining = "".join(axis for axis in axis_order if axis not in selected)
    if not remaining:
        raise ValueError("Dimension selection must leave at least one output axis.")
    return remaining


def _validate_index(axis: str, index: int, size: int) -> None:
    if isinstance(index, bool) or not isinstance(index, int):
        raise TypeError(f"{axis} index must be an integer, got {index!r}.")
    if index < 0 or index >= size:
        raise IndexError(f"{axis} index {index} is out of range for axis size {size}.")


def _validate_bound(axis: str, name: str, value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{axis} {name} must be an integer, got {value!r}.")
