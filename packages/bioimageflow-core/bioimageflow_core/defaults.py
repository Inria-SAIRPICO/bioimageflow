"""Owned semantic snapshots without copying local runtime resource owners."""

from dataclasses import fields, is_dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Optional

import numpy as np

from .types import SharedArray


def snapshot_value(value: Any, _active: Optional[set[int]] = None) -> Any:
    """Detach semantic values; a bound reference keeps its existing local lease.

    Explicit resource handles and arbitrary objects are not definition values.
    Callers handling a DataFrame must preserve its dtype/index and snapshot its
    object cells rather than pass the frame through this worker-safe helper.
    """
    if value is None or isinstance(
        value, (bool, int, float, complex, str, bytes, Path, Enum, SharedArray)
    ):
        return value
    if isinstance(value, np.generic):
        if value.dtype.hasobject:
            raise TypeError("Object-containing defaults cannot be captured")
        return value.copy()
    if isinstance(value, np.ndarray):
        if value.dtype.hasobject:
            raise TypeError("Object-containing defaults cannot be captured")
        return value.copy()
    active = set() if _active is None else _active
    identity = id(value)
    if identity in active:
        raise TypeError("Cyclic definition values cannot be captured")
    active.add(identity)
    try:
        if isinstance(value, dict):
            return {
                snapshot_value(key, active): snapshot_value(item, active)
                for key, item in value.items()
            }
        if isinstance(value, (list, tuple, set, frozenset)):
            return type(value)(snapshot_value(item, active) for item in value)
        from .environment import EnvironmentSpec

        if isinstance(value, EnvironmentSpec):
            return EnvironmentSpec(
                name=value.name,
                dependencies=value.dependencies,
                allow_flexible_versions=value.allow_flexible_versions,
            )
        if (
            is_dataclass(value)
            and not isinstance(value, type)
            and getattr(value, "__dataclass_params__").frozen
        ):
            return type(value)(
                **{
                    field.name: snapshot_value(getattr(value, field.name), active)
                    for field in fields(value)
                    if field.init
                }
            )
        raise TypeError(f"Unsupported definition value: {type(value).__name__}")
    finally:
        active.remove(identity)
