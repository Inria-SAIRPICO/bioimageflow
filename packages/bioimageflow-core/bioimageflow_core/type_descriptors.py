"""Finite current annotation grammar, independent of display labels."""

from __future__ import annotations

from enum import Enum
from pathlib import Path
import types
from typing import Annotated, Any, Literal, Union, cast, get_args, get_origin

import numpy as np
from .types import SharedArray

_ATOMS = {
    "any": Any,
    "none": type(None),
    "bool": bool,
    "int": int,
    "float": float,
    "complex": complex,
    "str": str,
    "bytes": bytes,
    "Path": Path,
    "SharedArray": SharedArray,
    "ndarray": np.ndarray,
}


def encode_annotation(annotation: Any) -> dict[str, Any]:
    """Capture supported semantic annotation; arbitrary classes cannot leak."""
    origin, args = get_origin(annotation), get_args(annotation)
    if origin is Annotated:
        return encode_annotation(args[0])
    for name, value in _ATOMS.items():
        if annotation is value:
            return {"kind": name}
    if origin is Union or (getattr(types, "UnionType", None) is not None and origin is getattr(types, "UnionType", None)):
        return {"kind": "union", "members": [encode_annotation(item) for item in args]}
    if (
        origin is Literal
        or isinstance(annotation, type)
        and issubclass(annotation, Enum)
    ):
        values = (
            list(args) if origin is Literal else [item.value for item in annotation]
        )
        if not values or any(
            type(item) not in (str, int, float, bool, type(None)) for item in values
        ):
            raise TypeError("Literal/Enum annotations require primitive choices")
        return {"kind": "literal", "values": values}
    container = origin or annotation
    if container in (list, tuple, dict):
        if container is list:
            return {"kind": "list", "item": encode_annotation(args[0] if args else Any)}
        if container is dict:
            key, value = args if args else (Any, Any)
            return {
                "kind": "dict",
                "key": encode_annotation(key),
                "value": encode_annotation(value),
            }
        variadic = not args or args[-1] is Ellipsis
        items = args[:1] if variadic and args else args or (Any,)
        return {
            "kind": "tuple",
            "items": [encode_annotation(item) for item in items],
            "variadic": variadic,
        }
    raise TypeError(f"Unsupported portable annotation: {annotation!r}")


def decode_annotation(spec: Any) -> Any:
    """Admit exact descriptor fields without evaluating Python expressions."""
    if type(spec) is not dict or type(spec.get("kind")) is not str:
        raise ValueError("Annotation descriptor requires a kind")
    kind = spec["kind"]
    if kind in _ATOMS and set(spec) == {"kind"}:
        return _ATOMS[kind]
    if kind == "union" and set(spec) == {"kind", "members"}:
        members = spec["members"]
        if type(members) is not list or len(members) < 2:
            raise ValueError("Union descriptor requires multiple members")
        return cast(Any, Union)[tuple(decode_annotation(item) for item in members)]
    if kind == "literal" and set(spec) == {"kind", "values"}:
        values = spec["values"]
        if (
            type(values) is not list
            or not values
            or any(
                type(item) not in (str, int, float, bool, type(None)) for item in values
            )
        ):
            raise ValueError("Literal descriptor requires primitive choices")
        return cast(Any, Literal)[tuple(values)]
    if kind == "list" and set(spec) == {"kind", "item"}:
        return cast(Any, list)[decode_annotation(spec["item"])]
    if kind == "dict" and set(spec) == {"kind", "key", "value"}:
        return cast(Any, dict)[
            decode_annotation(spec["key"]), decode_annotation(spec["value"])
        ]
    if kind == "tuple" and set(spec) == {"kind", "items", "variadic"}:
        items = spec["items"]
        if type(items) is not list or not items or type(spec["variadic"]) is not bool:
            raise ValueError("Tuple descriptor has invalid members")
        values = tuple(decode_annotation(item) for item in items)
        if spec["variadic"]:
            if len(values) != 1:
                raise ValueError("Variadic tuple requires one item type")
            return cast(Any, tuple)[values[0], ...]
        return cast(Any, tuple)[values]
    raise ValueError(f"Malformed/unsupported annotation descriptor: {kind!r}")
