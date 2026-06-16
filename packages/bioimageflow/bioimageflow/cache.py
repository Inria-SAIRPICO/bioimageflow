"""Hashing, caching, and provenance."""

import hashlib
import json
import os
import shutil
from enum import Enum
from pathlib import Path
from typing import Any

import pandas as pd

from bioimageflow.storage import ensure_dirs, find_hash_dir, create_hash_dir
from bioimageflow.storage_v1 import (
    CacheCorruptionError,
    RecordManifest,
    StorageV1,
    make_record_id,
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
    df_save = _prepare_dataframe_for_parquet(df)
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


def _prepare_dataframe_for_parquet(df: pd.DataFrame) -> pd.DataFrame:
    # Parquet requires Arrow-serializable types — convert Path/SharedArray-like
    # objects to strings while preserving ordinary scalar values.
    df_save = df.copy()
    for col in df_save.columns:
        if df_save[col].dtype == object or pd.api.types.is_string_dtype(df_save[col]):
            df_save[col] = df_save[col].apply(
                lambda v: str(v) if not isinstance(v, (str, int, float, bool, type(None))) else v
            )
    return df_save


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def dataframe_v1_result_key(node_name: str, sig_hash: str) -> str:
    """Return the transitional v1 result key for a DataFrameTool node."""
    return make_result_key(
        {
            "kind": "dataframe_tool",
            "node": node_name,
            "signature_hash": sig_hash,
        }
    )


def _dataframe_v1_record_path(storage: StorageV1, result_key: str, record_id: str) -> Path:
    return storage.result_dir(result_key) / "records" / record_id / "dataframe.parquet"


def dataframe_v1_lookup(
    storage_path: str | Path,
    node_name: str,
    sig_hash: str,
) -> pd.DataFrame | None:
    """Load a DataFrameTool v1 cache hit, or return ``None`` on miss."""
    storage = StorageV1(storage_path)
    result_key = dataframe_v1_result_key(node_name, sig_hash)
    pointer = storage.load_current(result_key)
    if pointer is None:
        return None
    try:
        return cache_load(_dataframe_v1_record_path(storage, result_key, pointer.record_id))
    except Exception as exc:
        raise CacheCorruptionError("Cached v1 dataframe is unreadable.") from exc


def dataframe_v1_publish(
    storage_path: str | Path,
    node_name: str,
    sig_hash: str,
    df: pd.DataFrame,
) -> pd.DataFrame:
    """Publish a DataFrameTool result through the v1 immutable record model."""
    storage = StorageV1(storage_path)
    result_key = dataframe_v1_result_key(node_name, sig_hash)
    attempt_id = storage.new_attempt_id()
    run_id = f"run_{attempt_id}"
    result_dir = storage.result_dir(result_key)
    staging_dir = result_dir / "attempts" / attempt_id / "staging"
    staging_dir.mkdir(parents=True, exist_ok=True)
    staging_parquet = staging_dir / "dataframe.parquet"
    _prepare_dataframe_for_parquet(df).to_parquet(staging_parquet, index=True)
    dataframe_digest = _file_sha256(staging_parquet)
    manifest_material = {
        "schema": "bioimageflow.cache.record.v1",
        "result_key": result_key,
        "dataframe": {
            "path": "dataframe.parquet",
            "digest": dataframe_digest,
        },
        "outputs": [],
    }
    record_id = make_record_id(manifest_material)
    records_dir = result_dir / "records"
    if records_dir.exists() or records_dir.is_symlink():
        try:
            records_dir.resolve().relative_to(result_dir.resolve())
        except ValueError as exc:
            raise CacheCorruptionError("Records directory escapes result directory.") from exc
        if records_dir.is_symlink():
            raise CacheCorruptionError("Records directory must not be a symlink.")
    else:
        records_dir.mkdir(parents=True)
    record_dir = records_dir / record_id
    if record_dir.exists() or record_dir.is_symlink():
        try:
            record_dir.resolve().relative_to((result_dir / "records").resolve())
        except ValueError as exc:
            raise CacheCorruptionError("Record directory escapes records directory.") from exc
        if record_dir.is_symlink():
            raise CacheCorruptionError("Record directory must not be a symlink.")
    else:
        record_dir.mkdir(parents=True)
    record_parquet = record_dir / "dataframe.parquet"
    if not record_parquet.exists():
        tmp_parquet = record_dir / f".dataframe.{attempt_id}.tmp"
        shutil.copy2(staging_parquet, tmp_parquet)
        os.replace(tmp_parquet, record_parquet)
    manifest = RecordManifest(
        result_key=result_key,
        record_id=record_id,
        dataframe_digest=dataframe_digest,
        outputs=[],
    )
    manifest_path = record_dir / "manifest.json"
    if not manifest_path.exists():
        tmp_manifest = record_dir / f".manifest.{attempt_id}.tmp"
        tmp_manifest.write_text(json.dumps(manifest.to_dict(), indent=2, sort_keys=True))
        os.replace(tmp_manifest, manifest_path)
    pointer = storage.select_current_record(
        result_key,
        candidate_record_id=record_id,
        attempt_id=attempt_id,
        run_id=run_id,
    )
    try:
        return cache_load(_dataframe_v1_record_path(storage, result_key, pointer.record_id))
    except Exception as exc:
        raise CacheCorruptionError("Published v1 dataframe is unreadable.") from exc
