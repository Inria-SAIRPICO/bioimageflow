"""One current typed processing value grammar; no memory attachment here."""

from __future__ import annotations
from pathlib import Path
from typing import Any, cast
import numpy as np
from bioimageflow_core.shm import _shared_memory_dtype
from bioimageflow_core.types import SharedArray

_LEAVES = (type(None), bool, int, float, str, bytes)


def _fields(value: dict, fields: set[str]) -> None:
    if set(value) != fields:
        raise ValueError("Typed processing value fields do not match its kind.")


def _array(value: Any) -> np.ndarray:
    if not isinstance(value, np.ndarray):
        raise ValueError("NumPy value must contain an array.")
    _shared_memory_dtype(value.dtype)
    if value.dtype.metadata:
        raise ValueError("NumPy dtype metadata is not supported in processing values.")
    return value


def _reference(value: dict) -> SharedArray:
    _fields(value, {"kind", "name", "shape", "dtype"})
    name = value["name"]
    if (
        type(name) is not str
        or not name
        or name != name.strip()
        or any(ord(char) < 32 for char in name)
    ):
        raise ValueError("SharedArray name must be normalized nonempty text.")
    shape = value["shape"]
    if type(shape) is not list or any(type(n) is not int or n < 0 for n in shape):
        raise ValueError("SharedArray shape must contain nonnegative integers.")
    dtype = value["dtype"]
    if type(dtype) is not str:
        raise ValueError("SharedArray dtype must be a string.")
    _shared_memory_dtype(dtype)
    return SharedArray(name=name, shape=tuple(shape), dtype=dtype)


def encode_processing_value(value: Any, _seen: set[int] | None = None) -> Any:
    seen = set() if _seen is None else _seen
    if isinstance(value, np.str_):
        return str(value)
    if isinstance(value, np.bytes_):
        return bytes(value)
    if isinstance(value, np.generic):
        array = _array(np.asarray(value))
        if array.dtype.kind not in "biufc":
            raise TypeError("Only numeric NumPy scalars are processing values.")
        return {"kind": "numpy_scalar", "value": array.copy()}
    if value is None:
        return None
    for primitive in (bool, int, float, str, bytes):
        if isinstance(value, primitive):
            return primitive(cast(Any, value))
    if isinstance(value, Path):
        return {"kind": "path", "value": str(value)}
    if isinstance(value, SharedArray):
        node = {
            "kind": "shared_array",
            "name": value.name,
            "shape": list(value.shape),
            "dtype": value.dtype,
        }
        _reference(node)
        return node
    if isinstance(value, np.ndarray):
        return {"kind": "ndarray", "value": _array(value).copy(order="K")}
    if type(value) not in (dict, list, tuple):
        raise TypeError(f"Unsupported processing value type: {type(value).__name__}.")
    identity = id(value)
    if identity in seen:
        raise ValueError("Cyclic processing values are not supported.")
    seen.add(identity)
    try:
        if type(value) is dict:
            pairs = []
            for key, item in value.items():
                if type(key) not in _LEAVES:
                    raise TypeError("Processing dictionaries require primitive keys.")
                pairs.append([key, encode_processing_value(item, seen)])
            return {"kind": "dict", "items": pairs}
        return {
            "kind": "list" if type(value) is list else "tuple",
            "items": [encode_processing_value(item, seen) for item in value],
        }
    finally:
        seen.remove(identity)


def decode_processing_value(value: Any, _seen: set[int] | None = None) -> Any:
    if type(value) in _LEAVES:
        return value
    if type(value) is not dict or type(value.get("kind")) is not str:
        raise ValueError("Processing value is not a declared primitive or typed node.")
    seen = set() if _seen is None else _seen
    identity = id(value)
    if identity in seen:
        raise ValueError("Cyclic processing descriptors are not supported.")
    seen.add(identity)
    try:
        kind = value["kind"]
        if kind == "shared_array":
            return _reference(value)
        if kind in ("ndarray", "numpy_scalar"):
            _fields(value, {"kind", "value"})
            array = _array(value["value"])
            if kind == "numpy_scalar":
                if array.ndim != 0 or array.dtype.kind not in "biufc":
                    raise ValueError(
                        "Numeric scalar requires a numeric zero-dimensional array."
                    )
                return array[()]
            return array
        if kind == "path":
            _fields(value, {"kind", "value"})
            text = value["value"]
            if type(text) is not str or not text or "\x00" in text:
                raise ValueError("Path value must contain valid nonempty text.")
            return Path(text)
        if kind not in ("dict", "list", "tuple"):
            raise ValueError(f"Unknown processing value kind: {kind!r}.")
        _fields(value, {"kind", "items"})
        items = value["items"]
        if type(items) is not list:
            raise ValueError("Processing container items must be an array.")
        if kind == "dict":
            result = {}
            for pair in items:
                if (
                    type(pair) is not list
                    or len(pair) != 2
                    or type(pair[0]) not in _LEAVES
                ):
                    raise ValueError(
                        "Processing dictionary item must be a primitive-key pair."
                    )
                key = pair[0]
                if key in result:
                    raise ValueError("Processing dictionary contains duplicate keys.")
                result[key] = decode_processing_value(pair[1], seen)
            return result
        decoded = [decode_processing_value(item, seen) for item in items]
        return decoded if kind == "list" else tuple(decoded)
    finally:
        seen.remove(identity)
