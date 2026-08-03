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
    if isinstance(value, bool):
        return {"__type__": "bool", "value": value}
    if isinstance(value, int):
        return {"__type__": "int", "value": value}
    if isinstance(value, float):
        return {"__type__": "float", "value": value}
    if isinstance(value, str):
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
    t = data["__type__"]
    v = data["value"]
    if t == "none":
        return None
    if t == "bool":
        return bool(v)
    if t == "int":
        return int(v)
    if t == "float":
        return float(v)
    if t == "str":
        return str(v)
    if t == "path":
        return Path(v)
    if t == "tuple":
        return tuple(deserialize_constant(item) for item in v)
    if t == "list":
        return [deserialize_constant(item) for item in v]
    if t == "dict":
        return {
            deserialize_constant(entry["key"]): deserialize_constant(entry["value"])
            for entry in v
        }
    raise ValueError(f"Unknown workflow constant type: {t!r}.")
