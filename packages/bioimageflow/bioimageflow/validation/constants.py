"""Focused orchestrator validation behavior."""

from __future__ import annotations

from .common import (
    Any,
    Path,
)


def serialize_constant(value: Any) -> dict[str, Any]:
    """Serialize a tool-parameter constant to a JSON-safe envelope.

    The output is a dict ``{"__type__": <name>, "value": <payload>}`` that
    round-trips through :func:`deserialize_constant`. This is the format
    used inside the ``constants`` block of a workflow's
    :meth:`Workflow.to_dict` output.

    Supported types and their envelopes:

    - ``None``   → ``{"__type__": "none", "value": None}``
    - ``bool``   → ``{"__type__": "bool", "value": <bool>}``
    - ``int``    → ``{"__type__": "int", "value": <int>}``
    - ``float``  → ``{"__type__": "float", "value": <float>}``
    - :class:`pathlib.Path` → ``{"__type__": "path", "value": <str>}``
    - ``list`` and ``tuple`` recursively encode every item;
    - ``dict`` recursively encodes keys and values as ordered entries.

    Unsupported values are rejected instead of being stringified lossily.
    """
    if value is None:
        return {"__type__": "none", "value": None}
    if type(value) is bool:
        return {"__type__": "bool", "value": value}
    if type(value) is int:
        return {"__type__": "int", "value": value}
    if type(value) is float:
        return {"__type__": "float", "value": value}
    if type(value) is str:
        return {"__type__": "str", "value": value}
    if isinstance(value, Path):
        return {"__type__": "path", "value": value.as_posix()}
    if isinstance(value, (list, tuple)):
        return {
            "__type__": type(value).__name__,
            "value": [serialize_constant(item) for item in value],
        }
    if isinstance(value, dict):
        return {
            "__type__": "dict",
            "value": [
                {
                    "key": serialize_constant(key),
                    "value": serialize_constant(item),
                }
                for key, item in value.items()
            ],
        }
    raise TypeError(f"Unsupported workflow constant type: {type(value).__name__}.")


def deserialize_constant(data: dict[str, Any]) -> Any:
    """Inverse of :func:`serialize_constant`.

    Expects a typed envelope ``{"__type__": <name>, "value": <payload>}``
    produced by :func:`serialize_constant`. Unknown ``__type__`` values
    are rejected.
    """
    if type(data) is not dict or set(data) != {"__type__", "value"}:
        raise ValueError("Workflow constant must have exact type/value fields")
    t, v = data["__type__"], data["value"]
    if type(t) is not str:
        raise ValueError("Workflow constant kind must be a string")
    if t == "none":
        if v is not None:
            raise ValueError("none constant requires None")
        return None
    scalar_types = {"bool": bool, "int": int, "float": float, "str": str}
    if t in scalar_types:
        if type(v) is not scalar_types[t]:
            raise ValueError(f"{t} constant has an invalid payload type")
        return v
    if t == "path":
        if type(v) is not str:
            raise ValueError("path constant requires a string")
        return Path(v)
    if t in {"tuple", "list"}:
        if type(v) is not list:
            raise ValueError(f"{t} constant requires an array")
        values = [deserialize_constant(item) for item in v]
        return tuple(values) if t == "tuple" else values
    if t == "dict":
        if type(v) is not list:
            raise ValueError("dict constant requires an entry array")
        result = {}
        for entry in v:
            if type(entry) is not dict or set(entry) != {"key", "value"}:
                raise ValueError("dict constant entries require exact key/value fields")
            key = deserialize_constant(entry["key"])
            try:
                if key in result:
                    raise ValueError("dict constant contains duplicate keys")
                result[key] = deserialize_constant(entry["value"])
            except TypeError as error:
                raise ValueError("dict constant key is unhashable") from error
        return result
    raise ValueError(f"Unknown workflow constant type: {t!r}.")
