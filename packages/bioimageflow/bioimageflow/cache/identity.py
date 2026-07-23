"""Focused cache operations for identity."""

from __future__ import annotations

from .common import (
    Any,
    Enum,
    Path,
    hashlib,
    json,
    make_result_key,
)


def _normalize_dependency_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): _normalize_dependency_value(item)
            for key, item in sorted(value.items())
        }
    if isinstance(value, list):
        normalized_items = [_normalize_dependency_value(item) for item in value]
        serialized_items = {
            json.dumps(item, sort_keys=True, separators=(",", ":")): item
            for item in normalized_items
        }
        return [serialized_items[key] for key in sorted(serialized_items)]
    if isinstance(value, str):
        return value.strip()
    return value


def normalize_dependencies(dependencies: dict[str, Any]) -> dict[str, Any]:
    """Normalize dependencies for consistent hashing."""
    normalized: dict[str, Any] = {}
    for key, value in sorted(dependencies.items()):
        normalized[key] = _normalize_dependency_value(value)
    return normalized


def compute_env_hash(dependencies: dict[str, Any]) -> str:
    """SHA256 of normalized dependencies."""
    normalized = normalize_dependencies(dependencies)
    data = json.dumps(normalized, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(data.encode()).hexdigest()


def deterministic_serialize(obj: Any) -> str:
    """Serialize an object deterministically for hashing."""

    def _default(o: Any) -> Any:
        if isinstance(o, Path):
            return o.as_posix()
        if isinstance(o, (set, frozenset)):
            return sorted(str(x) for x in o)
        if isinstance(o, tuple):
            return list(o)
        if isinstance(o, Enum):
            return o.value
        if hasattr(o, "__dataclass_fields__"):
            return {k: getattr(o, k) for k in o.__dataclass_fields__}
        raise TypeError(
            f"Cannot serialize {type(o).__name__} for hashing. "
            f"Add explicit handling in deterministic_serialize()."
        )

    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=_default)


def compute_signature_hash(
    tool_name: str,
    tool_version: str,
    env_hash: str,
    resolved_params: Any,
    upstream_hashes: dict[str, Any],
    source_hash: str | None = None,
) -> str:
    """Compute the logical digest for a node."""
    parts = [tool_name, str(tool_version), env_hash]
    if source_hash is not None:
        parts.append(source_hash)
    parts.append(deterministic_serialize(resolved_params))
    for name, identity in sorted(upstream_hashes.items()):
        parts.append(f"{name}:{deterministic_serialize(identity)}")
    combined = "|".join(parts)
    return hashlib.sha256(combined.encode()).hexdigest()


def dataframe_result_key(node_name: str, sig_hash: str) -> str:
    """Return the result key for a DataFrameTool node."""
    return make_result_key(
        {
            "kind": "dataframe_tool",
            "node": node_name,
            "logical_digest": sig_hash,
        }
    )


def processing_result_key(node_name: str, sig_hash: str) -> str:
    """Return the result key for a ProcessingTool node."""
    return make_result_key(
        {
            "kind": "processing_tool",
            "node": node_name,
            "logical_digest": sig_hash,
        }
    )
