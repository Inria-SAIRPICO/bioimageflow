"""One portable record-cell adapter over Core's finite value traversal."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import struct
from typing import Any, Callable, cast

import numpy as np
from bioimageflow_core import SharedArray, accept_native_array, decode_processing_value, encode_processing_value

AssetEncoder = Callable[[Any], dict[str, Any]]
AssetDecoder = Callable[[dict[str, Any]], Any]


def needs_portable_cell(value: Any) -> bool:
    return type(value) in (dict, list, tuple, bytes) or isinstance(value, np.generic) and value.dtype.kind == "c"


def _encode_scalar(value: Any) -> dict[str, Any]:
    if isinstance(value, np.generic):
        return {"kind": "scalar", "type": "numpy", "dtype": value.dtype.str,
                "value": value.tobytes().hex()}
    if value is None:
        return {"kind": "scalar", "type": "null", "value": None}
    if type(value) is bool:
        return {"kind": "scalar", "type": "bool", "value": value}
    if type(value) is int:
        return {"kind": "scalar", "type": "int", "value": str(value)}
    if type(value) is float:
        return {"kind": "scalar", "type": "float", "value": struct.pack(">d", value).hex()}
    if type(value) is bytes:
        return {"kind": "scalar", "type": "bytes", "value": value.hex()}
    if type(value) is str:
        return {"kind": "scalar", "type": "str", "value": value}
    raise TypeError(f"Unsupported portable scalar: {type(value).__name__}")


def _decode_scalar(node: dict[str, Any]) -> Any:
    kind, value = node.get("type"), node.get("value")
    fields = {"kind", "type", "value", "dtype"} if kind == "numpy" else {"kind", "type", "value"}
    if set(node) != fields or node.get("kind") != "scalar":
        raise ValueError("Invalid portable scalar fields")
    if kind == "null" and value is None:
        return None
    if kind == "bool" and type(value) is bool:
        return value
    if kind == "str" and type(value) is str:
        return value
    if kind == "int" and type(value) is str:
        result = int(value)
        if str(result) == value:
            return result
    if kind in {"float", "bytes", "numpy"} and type(value) is str:
        raw = bytes.fromhex(value)
        if raw.hex() != value:
            raise ValueError("Noncanonical portable scalar bytes")
        if kind == "bytes":
            return raw
        if kind == "float" and len(raw) == 8:
            return struct.unpack(">d", raw)[0]
        if kind == "numpy":
            dtype = np.dtype(node["dtype"])
            if dtype.kind in "biufc" and not dtype.hasobject and dtype.metadata is None and len(raw) == dtype.itemsize:
                return np.frombuffer(raw, dtype=dtype)[0]
    raise ValueError("Invalid portable scalar value")


def encode_cell(value: Any, *, encode_asset: AssetEncoder) -> Any:
    def leaf(value: Any, *, is_key: bool) -> dict[str, Any]:
        if isinstance(value, (SharedArray, np.ndarray, Path)):
            if is_key:
                raise ValueError("Portable asset cannot be a dictionary key")
            return encode_asset(value)
        return _encode_scalar(value)
    return encode_processing_value(value, encode_leaf=leaf)


def decode_cell(node: Any, *, decode_asset: AssetDecoder) -> Any:
    def leaf(value: Any, *, is_key: bool) -> Any:
        if type(value) is not dict:
            raise ValueError("Portable leaves require explicit typed nodes")
        if value.get("kind") == "scalar":
            return _decode_scalar(value)
        if is_key:
            raise ValueError("Portable asset cannot be a dictionary key")
        if value.get("kind") != "asset" or set(value) != {"kind", "role", "path"}:
            raise ValueError("Invalid portable asset fields")
        if value["role"] not in {"native_array", "shared_array", "owned_path", "external_path"} or type(value["path"]) is not str:
            raise ValueError("Invalid portable asset role/path")
        return decode_asset(value)
    return decode_processing_value(node, decode_leaf=leaf)


def cell_text(node: Any) -> str:
    return json.dumps(node, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def parse_cell(text: Any) -> Any:
    if type(text) is not str:
        raise ValueError("Portable record cell must be JSON text")
    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate portable descriptor field")
            result[key] = value
        return result
    def invalid(value: str) -> Any:
        raise ValueError(f"Noncanonical JSON number: {value}")
    return json.loads(text, object_pairs_hook=unique, parse_constant=invalid)


def portable_identity(value: Any) -> Any:
    def asset(item: Any) -> dict[str, Any]:
        if isinstance(item, SharedArray):
            if item.bound_owner is None:
                raise ValueError("Shared cell identity requires an admitted owner")
            return {"kind": "shared_array", **item.bound_owner.content_identity(item)}
        if isinstance(item, np.ndarray):
            array = accept_native_array(item)
            return {"kind": "native_array", "dtype": str(array.dtype), "shape": list(array.shape),
                    "sha256": hashlib.sha256(memoryview(cast(Any, array))).hexdigest()}
        return {"kind": "path", "value": str(item.expanduser().absolute())}
    return encode_cell(value, encode_asset=asset)


def admit_record_cell(
    text: Any, outputs: list[dict[str, Any]], *, column: str, row_index: str,
    hydrate_asset: Callable[[dict[str, Any], str], Any] | None = None,
) -> tuple[Any, set[str]]:
    """Validate every leaf role/cell binding before any optional attachment."""
    referenced: set[str] = set()
    def asset(node: dict[str, Any]) -> Any:
        role, path = node["role"], node["path"]
        if role == "external_path":
            if not Path(path).is_absolute() or "\x00" in path or not any(
                output.get("kind") == "external_path" and output.get("path") == path
                for output in outputs
            ):
                raise ValueError("Portable external path has no manifest authority")
            return Path(path)
        if not path or path.startswith("/") or "\\" in path or any(part in {"", ".", ".."} for part in path.split("/")):
            raise ValueError("Unsafe portable asset path")
        matches = [output for output in outputs if output.get("kind") == "owned_asset" and output.get("path") == path]
        if len(matches) != 1:
            raise ValueError("Portable asset has no unique manifest authority")
        output = matches[0]
        if role in {"native_array", "shared_array"}:
            metadata = output.get("array", {})
            if output.get("asset_role") != role or metadata.get("column") != column or metadata.get("row_index") != row_index:
                raise ValueError("Portable array role/cell binding mismatch")
            referenced.add(path)
        elif output.get("asset_role") is not None:
            raise ValueError("Portable path uses an array asset role")
        return Path(path) if hydrate_asset is None else hydrate_asset(output, role)
    # A pure full pass refuses malformed later leaves before earlier attachment.
    node = parse_cell(text)
    decoded = None
    if hydrate_asset is None:
        decoded = decode_cell(node, decode_asset=asset)
    if hydrate_asset is not None:
        admit_record_cell(text, outputs, column=column, row_index=row_index)
        decoded = decode_cell(node, decode_asset=asset)
    return decoded, referenced
