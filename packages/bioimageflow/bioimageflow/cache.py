"""Hashing, caching, and provenance."""

import hashlib
import json
import time
from enum import Enum
from pathlib import Path
from typing import Any

import pandas as pd

from bioimageflow.storage import ensure_dirs, find_hash_dir, create_hash_dir


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
        return [
            serialized_items[key]
            for key in sorted(serialized_items)
        ]
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
        if hasattr(o, '__dataclass_fields__'):
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
    upstream_hashes: dict[str, str],
    source_hash: str | None = None,
) -> str:
    """Compute the signature hash for a node."""
    parts = [tool_name, str(tool_version), env_hash]
    if source_hash is not None:
        parts.append(source_hash)
    parts.append(deterministic_serialize(resolved_params))
    # Sort upstream hashes by node name for determinism
    for name, h in sorted(upstream_hashes.items()):
        parts.append(f"{name}:{h}")
    combined = "|".join(parts)
    return hashlib.sha256(combined.encode()).hexdigest()


def cache_lookup(node_dir: Path, sig_hash: str) -> Path | None:
    """Check if a cached result exists. Returns Path to the cache file or None.

    Prefers Parquet; falls back to CSV for backwards compatibility.
    """
    hash_dir = find_hash_dir(node_dir, sig_hash)
    if hash_dir is not None:
        parquet_path = hash_dir / "dataframe.parquet"
        if parquet_path.exists():
            return parquet_path
        csv_path = hash_dir / "dataframe.csv"
        if csv_path.exists():
            return csv_path
    return None


def cache_save(
    node_dir: Path,
    sig_hash: str,
    df: pd.DataFrame,
    metadata: dict[str, Any] | None = None,
    parameters: dict[str, Any] | None = None,
    hash_dir: Path | None = None,
) -> None:
    """Save a DataFrame and metadata to the cache.

    Writes Parquet (lossless) as the primary format and CSV as a
    human-readable secondary output.

    If *hash_dir* is provided (already created during execution), reuse it.
    Otherwise create a new timestamped hash directory.
    """
    if hash_dir is None:
        hash_dir = create_hash_dir(node_dir, sig_hash)
    else:
        ensure_dirs(hash_dir)
    # Parquet requires Arrow-serializable types — convert Path/SharedArray to str
    df_save = df.copy()
    for col in df_save.columns:
        if df_save[col].dtype == object or pd.api.types.is_string_dtype(df_save[col]):
            df_save[col] = df_save[col].apply(
                lambda v: str(v) if not isinstance(v, (str, int, float, bool, type(None))) else v
            )
    df_save.to_parquet(hash_dir / "dataframe.parquet", index=True)
    df_save.to_csv(hash_dir / "dataframe.csv", index=True)
    if metadata:
        (hash_dir / "metadata.json").write_text(
            json.dumps(metadata, indent=2, default=str)
        )
    if parameters:
        (hash_dir / "parameters.json").write_text(
            json.dumps(parameters, indent=2, default=str)
        )


def cache_load(cache_path: Path) -> pd.DataFrame:
    """Load a DataFrame from cache.

    Accepts either a ``.parquet`` or ``.csv`` path.
    """
    if cache_path.suffix == ".parquet":
        df = pd.read_parquet(cache_path)
    else:
        # Legacy CSV fallback
        df = pd.read_csv(cache_path, index_col=0, keep_default_na=False)
        # Restore numeric columns where possible
        for col in df.columns:
            if pd.api.types.is_string_dtype(df[col]):
                try:
                    df[col] = pd.to_numeric(df[col])
                except (ValueError, TypeError):
                    pass
    df.index = df.index.astype(str)
    return df


def cleanup_cache(node_dir: Path, max_executions: int = 0, max_age: str | None = None) -> None:
    """Remove old hash dirs based on retention policy."""
    import shutil

    node_dir = Path(node_dir)
    if not node_dir.exists():
        return
    hash_dirs = sorted(
        [d for d in node_dir.iterdir() if d.is_dir()],
        key=lambda d: d.stat().st_mtime,
        reverse=True,
    )
    # max_executions=0 means keep only the latest; N means keep N total
    to_keep = max_executions if max_executions > 0 else 1
    for d in hash_dirs[to_keep:]:
        shutil.rmtree(d)

    if max_age is not None:
        now = time.time()
        if max_age.endswith('d'):
            max_seconds = int(max_age[:-1]) * 86400
        elif max_age.endswith('h'):
            max_seconds = int(max_age[:-1]) * 3600
        else:
            max_seconds = int(max_age)
        for d in node_dir.iterdir():
            if d.is_dir() and (now - d.stat().st_mtime) > max_seconds:
                shutil.rmtree(d)
