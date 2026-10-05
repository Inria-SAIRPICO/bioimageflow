"""One current typed processing value grammar; no memory attachment here."""

from __future__ import annotations
from pathlib import Path
from typing import Any, Protocol, cast
import numpy as np
from bioimageflow_core.shm import _shared_memory_dtype
from bioimageflow_core.types import SharedArray

_LEAVES = (type(None), bool, int, float, str, bytes)
_CONTAINERS = {"dict", "list", "tuple"}


class _LeafCodec(Protocol):
    def __call__(self, value: Any, *, is_key: bool) -> Any: ...


def _fields(value: dict, fields: set[str]) -> None:
    if set(value) != fields:
        raise ValueError("Typed processing value fields do not match its kind.")


def _array(value: Any) -> np.ndarray:
    if not isinstance(value, np.ndarray):
        raise ValueError("NumPy value must contain an array.")
    _shared_memory_dtype(value.dtype)
    if _dtype_metadata(value.dtype):
        raise ValueError("NumPy dtype metadata is not supported in processing values.")
    return value


def _dtype_metadata(dtype: np.dtype) -> bool:
    if dtype.metadata:
        return True
    if dtype.subdtype is not None and _dtype_metadata(dtype.subdtype[0]):
        return True
    return any(_dtype_metadata(field[0]) for field in (dtype.fields or {}).values())


def accept_native_array(value: np.ndarray) -> np.ndarray:
    """Accept a C-layout native array with immutable data and independent metadata.

    Mutable producers are copied once into immutable bytes. Re-admission of an
    immutable bytes-backed C array creates only a new ndarray view descriptor.
    A readonly flag or readonly memoryview does not prove immutable backing.
    """
    array = _array(value)
    base: Any = array
    while isinstance(base, np.ndarray):
        base = base.base
    if isinstance(base, bytes) and array.flags.c_contiguous:
        return array.view(np.ndarray)
    return np.frombuffer(array.tobytes(order="C"), dtype=array.dtype).reshape(array.shape)


def accept_native_values(value: Any) -> Any:
    """Snapshot native leaves together; other leaves keep their existing authority."""
    memo: dict[int, np.ndarray] = {}
    visiting: set[int] = set()

    def walk(item: Any) -> Any:
        if isinstance(item, np.ndarray):
            key = id(item)
            if key not in memo:
                memo[key] = accept_native_array(item)
            return accept_native_array(memo[key])
        if type(item) not in (dict, list, tuple):
            return item
        key = id(item)
        if key in visiting:
            raise ValueError("Cyclic processing values are not supported.")
        visiting.add(key)
        try:
            if type(item) is dict:
                return {key: walk(child) for key, child in item.items()}
            children = [walk(child) for child in item]
            return children if type(item) is list else tuple(children)
        finally:
            visiting.remove(key)

    return walk(value)


def _reference(value: dict) -> SharedArray:
    _fields(value, {"kind", "name", "shape", "dtype", "scope_id"})
    name = value["name"]
    from bioimageflow_core._shared_storage import token
    token(name)  # bounded portable allocation token; validation performs no I/O
    shape = value["shape"]
    if type(shape) is not list or any(type(n) is not int or n < 0 for n in shape):
        raise ValueError("SharedArray shape must contain nonnegative integers.")
    dtype = value["dtype"]
    if type(dtype) is not str:
        raise ValueError("SharedArray dtype must be a string.")
    _shared_memory_dtype(dtype)
    scope_id = value["scope_id"]
    if type(scope_id) is not str or not scope_id or any(c not in "abcdefghijklmnopqrstuvwxyz0123456789_" for c in scope_id):
        raise ValueError("SharedArray scope identity must be a bounded token.")
    if len(scope_id) > 96:
        raise ValueError("SharedArray scope identity is too long.")
    return SharedArray(name=name, shape=tuple(shape), dtype=dtype, scope_id=scope_id)


def _admit_leaf(value: Any) -> Any:
    """Validate one live leaf without copying pixels or stripping its owner."""
    if isinstance(value, np.str_):
        return str(value)
    if isinstance(value, np.bytes_):
        return bytes(value)
    if isinstance(value, np.generic):
        array = _array(np.asarray(value))
        if array.dtype.kind not in "biufc":
            raise TypeError("Only numeric NumPy scalars are processing values.")
        return value
    if value is None:
        return None
    for primitive in (bool, int, float, str, bytes):
        if isinstance(value, primitive):
            return primitive(cast(Any, value))
    if isinstance(value, Path):
        if "\x00" in str(value):
            raise ValueError("Path value must contain valid nonempty text.")
        return value
    if isinstance(value, SharedArray):
        node = {
            "kind": "shared_array",
            "name": value.name,
            "shape": list(value.shape),
            "dtype": value.dtype,
            "scope_id": value.scope_id,
        }
        _reference(node)
        return value
    if isinstance(value, np.ndarray):
        return _array(value)
    raise TypeError(f"Unsupported processing value type: {type(value).__name__}.")


def _encode_leaf(value: Any, *, is_key: bool) -> Any:
    if type(value) in _LEAVES:
        return value
    if isinstance(value, Path):
        return {"kind": "path", "value": str(value)}
    if isinstance(value, SharedArray):
        return {"kind": "shared_array", "name": value.name,
                "shape": list(value.shape), "dtype": value.dtype, "scope_id": value.scope_id}
    if isinstance(value, np.generic):
        return {"kind": "numpy_scalar", "value": np.asarray(value).copy()}
    return {"kind": "ndarray", "value": value.copy(order="K")}


def _leaf_node(value: Any) -> None:
    if type(value) in _LEAVES:
        return
    if type(value) is not dict or type(value.get("kind")) is not str:
        raise ValueError("Processing leaf is not a declared primitive or typed node.")
    if value["kind"] in _CONTAINERS:
        raise ValueError("Processing leaf cannot contain a container node.")


def _decode_leaf(value: Any, *, is_key: bool) -> Any:
    if type(value) in _LEAVES:
        return value
    kind = value["kind"]
    if kind == "shared_array":
        return _reference(value)
    if kind in ("ndarray", "numpy_scalar"):
        _fields(value, {"kind", "value"})
        array = _array(value["value"])
        if kind == "numpy_scalar":
            if array.ndim != 0 or array.dtype.kind not in "biufc":
                raise ValueError("Numeric scalar requires a numeric zero-dimensional array.")
            return array[()]
        return array
    if kind == "path":
        _fields(value, {"kind", "value"})
        text = value["value"]
        if type(text) is not str or not text or "\x00" in text:
            raise ValueError("Path value must contain valid nonempty text.")
        return Path(text)
    raise ValueError(f"Unknown processing value kind: {kind!r}.")


def encode_processing_value(value: Any, *, encode_leaf: _LeafCodec | None = None) -> Any:
    """Encode one finite value; custom leaves retain live owner authority.

    The leaf callback receives ``is_key`` and cannot replace container grammar.
    The default callback preserves the current task/result transport.
    """
    codec = _encode_leaf if encode_leaf is None else encode_leaf
    seen: set[int] = set()

    def leaf(item: Any, *, is_key: bool) -> Any:
        encoded = codec(_admit_leaf(item), is_key=is_key)
        _leaf_node(encoded)
        return encoded

    def walk(item: Any) -> Any:
        if type(item) not in (dict, list, tuple):
            return leaf(item, is_key=False)
        identity = id(item)
        if identity in seen:
            raise ValueError("Cyclic processing values are not supported.")
        seen.add(identity)
        try:
            if type(item) is dict:
                pairs = []
                for key, child in item.items():
                    if type(key) not in _LEAVES:
                        raise TypeError("Processing dictionaries require primitive keys.")
                    pairs.append([leaf(key, is_key=True), walk(child)])
                return {"kind": "dict", "items": pairs}
            return {"kind": "list" if type(item) is list else "tuple",
                    "items": [walk(child) for child in item]}
        finally:
            seen.remove(identity)

    return walk(value)


def decode_processing_value(value: Any, *, decode_leaf: _LeafCodec | None = None) -> Any:
    """Decode finite containers and admit every reconstructed leaf without I/O.

    Custom leaf callbacks own their leaf metadata validation and effects.
    Key callbacks receive ``is_key=True`` before decoding any key leaf.
    """
    codec = _decode_leaf if decode_leaf is None else decode_leaf
    seen: set[int] = set()

    def leaf(item: Any, *, is_key: bool) -> Any:
        _leaf_node(item)
        decoded = codec(item, is_key=is_key)
        if is_key and type(decoded) not in _LEAVES:
            raise ValueError("Processing dictionaries require primitive keys.")
        return _admit_leaf(decoded)

    def walk(item: Any) -> Any:
        if type(item) in _LEAVES:
            return leaf(item, is_key=False)
        if type(item) is not dict or type(item.get("kind")) is not str:
            raise ValueError("Processing value is not a declared primitive or typed node.")
        kind = item["kind"]
        if kind not in _CONTAINERS:
            return leaf(item, is_key=False)
        identity = id(item)
        if identity in seen:
            raise ValueError("Cyclic processing descriptors are not supported.")
        seen.add(identity)
        try:
            _fields(item, {"kind", "items"})
            items = item["items"]
            if type(items) is not list:
                raise ValueError("Processing container items must be an array.")
            if kind == "dict":
                result = {}
                for pair in items:
                    if type(pair) is not list or len(pair) != 2:
                        raise ValueError("Processing dictionary item must be a primitive-key pair.")
                    key = leaf(pair[0], is_key=True)
                    if key in result:
                        raise ValueError("Processing dictionary contains duplicate keys.")
                    result[key] = walk(pair[1])
                return result
            decoded = [walk(child) for child in items]
            return decoded if kind == "list" else tuple(decoded)
        finally:
            seen.remove(identity)

    return walk(value)
