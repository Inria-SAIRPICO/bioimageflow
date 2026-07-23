"""Focused orchestrator validation behavior."""

from __future__ import annotations

from .common import (
    Any,
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
    - ``list``   → ``{"__type__": "list", "value": [...]}``
    - ``tuple``  → ``{"__type__": "tuple", "value": [...]}``
    - anything else (including :class:`pathlib.Path`, Pydantic models,
      enums, custom dataclasses) is **lossily** stringified via ``str()``
      and tagged ``{"__type__": "str", ...}``. Callers that need lossless
      round-trip for non-primitive values must serialize them at a
      higher layer.
    """
    if value is None:
        return {"__type__": "none", "value": None}
    if isinstance(value, bool):
        return {"__type__": "bool", "value": value}
    if isinstance(value, int):
        return {"__type__": "int", "value": value}
    if isinstance(value, float):
        return {"__type__": "float", "value": value}
    if isinstance(value, (list, tuple)):
        return {"__type__": type(value).__name__, "value": list(value)}
    return {"__type__": "str", "value": str(value)}


def deserialize_constant(data: dict[str, Any]) -> Any:
    """Inverse of :func:`serialize_constant`.

    Expects a typed envelope ``{"__type__": <name>, "value": <payload>}``
    produced by :func:`serialize_constant`. Unknown ``__type__`` values
    are coerced to ``str``.
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
    if t == "tuple":
        return tuple(v)
    if t == "list":
        return list(v)
    return str(v)
