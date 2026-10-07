"""Semantic definition values, independent of execution/resource handles."""
from __future__ import annotations

from dataclasses import dataclass, fields, is_dataclass
from enum import Enum
from pathlib import Path
import struct
from typing import Any

import numpy as np
import pandas as pd
from bioimageflow_core import IOModel
from bioimageflow_core.defaults import snapshot_value
from bioimageflow_core.types import SharedArray


def capture_value(value: Any) -> Any:
    if isinstance(value, pd.DataFrame):
        result = value.copy(deep=True)
        for column in range(len(result.columns)):
            if pd.api.types.is_object_dtype(result.dtypes.iloc[column]):
                for row in range(len(result)):
                    result.iat[row, column] = snapshot_value(result.iat[row, column])
        result.attrs = snapshot_value(value.attrs)
        return result
    return snapshot_value(value)


def capture_model(model: Any) -> Any:
    """Detach declaration mappings without changing the executable tool class."""
    if model is None:
        return None
    from bioimageflow.dataframe_tool import Passthrough
    namespace = {
        "__module__": model.__module__,
        "__annotations__": dict(model._get_all_annotations()),
        **model.capture_defaults(),
    }
    base = Passthrough if issubclass(model, Passthrough) else IOModel
    return type(model.__name__, (base,), namespace)


@dataclass(frozen=True)
class CapturedOutputDeclaration:
    """Exact nominal result authority alongside detached validation semantics."""

    nominal_type: type[IOModel] | None
    frozen_model: type[IOModel] | None

    def require_current_nominal(self, model: type[IOModel] | None) -> None:
        """Refuse finite declaration drift before a Direct callback executes."""
        from bioimageflow_core.declarations import describe_io_model

        if model is not self.frozen_model:
            raise ValueError("Captured output declaration replaced during execution")
        if self.nominal_type is None or self.frozen_model is None:
            return
        unchanged = _same_captured_value(
            describe_io_model(self.nominal_type), describe_io_model(self.frozen_model),
        ) and _same_captured_value(
            self.nominal_type.capture_defaults(), self.frozen_model.capture_defaults(),
        )
        if not unchanged:
            raise ValueError("Nominal output declaration changed after capture")


def capture_output_declaration(
    model: type[IOModel] | None,
    previous: CapturedOutputDeclaration | None = None,
) -> CapturedOutputDeclaration:
    nominal_type = (
        previous.nominal_type
        if previous is not None and model is previous.frozen_model
        else model
    )
    return CapturedOutputDeclaration(nominal_type, capture_model(model))


def _same_captured_value(current: Any, captured: Any) -> bool:
    """Compare supported definition snapshots without coercion or owner I/O."""
    from bioimageflow_core.environment import EnvironmentSpec

    if isinstance(current, EnvironmentSpec) and isinstance(captured, EnvironmentSpec):
        return (
            _same_captured_value(current.name, captured.name)
            and _same_captured_value(current.allow_flexible_versions, captured.allow_flexible_versions)
            and _same_captured_value(current.dependencies, captured.dependencies)
        )
    if type(current) is not type(captured):
        return False
    if current is captured:
        return True
    if isinstance(current, (Enum, SharedArray)):
        return False
    if type(current) in (bool, int, str, bytes) or isinstance(current, Path):
        return current == captured
    if type(current) is float:
        return struct.pack("!d", current) == struct.pack("!d", captured)
    if type(current) is complex:
        return struct.pack("!dd", current.real, current.imag) == struct.pack("!dd", captured.real, captured.imag)
    if isinstance(current, (np.ndarray, np.generic)):
        if current.dtype != captured.dtype or current.shape != captured.shape:
            return False
        if current.dtype.names and isinstance(current, (np.ndarray, np.void)):
            return all(
                _same_captured_value(current[name], captured[name])
                for name in current.dtype.names
            )
        return current.tobytes() == captured.tobytes()
    if isinstance(current, dict):
        return len(current) == len(captured) and all(
            _same_captured_value(key, old_key) and _same_captured_value(value, old_value)
            for (key, value), (old_key, old_value) in zip(current.items(), captured.items())
        )
    if isinstance(current, (list, tuple)):
        return len(current) == len(captured) and all(
            _same_captured_value(value, old_value)
            for value, old_value in zip(current, captured)
        )
    if isinstance(current, (set, frozenset)):
        unmatched = list(captured)
        for value in current:
            match = next((index for index, old in enumerate(unmatched)
                          if _same_captured_value(value, old)), None)
            if match is None:
                return False
            unmatched.pop(match)
        return not unmatched
    parameters = getattr(current, "__dataclass_params__", None)
    if is_dataclass(current) and not isinstance(current, type) and parameters is not None and parameters.frozen:
        return all(
            _same_captured_value(getattr(current, field.name), getattr(captured, field.name))
            for field in fields(current) if field.init
        )
    return False
