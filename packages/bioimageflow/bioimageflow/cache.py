"""Hashing, caching, and provenance."""

import hashlib
import json
import os
import re
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
    asset_digest_and_size,
    make_record_id,
    make_result_key,
    validate_relative_posix_path,
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


def processing_v1_result_key(node_name: str, sig_hash: str) -> str:
    """Return the transitional v1 result key for a ProcessingTool node."""
    return make_result_key(
        {
            "kind": "processing_tool",
            "node": node_name,
            "signature_hash": sig_hash,
        }
    )


def _write_processing_v1_result_metadata(
    result_dir: Path,
    *,
    node_name: str,
    sig_hash: str,
    result_key: str,
    attempt_id: str,
) -> None:
    metadata_path = result_dir / "result.json"
    metadata = {
        "schema": "bioimageflow.cache.result.v1",
        "kind": "processing_tool",
        "node": node_name,
        "signature_hash": sig_hash,
        "result_key": result_key,
    }
    if not metadata_path.exists():
        tmp_path = result_dir / f".result.{attempt_id}.json.tmp"
        tmp_path.write_text(json.dumps(metadata, indent=2, sort_keys=True))
        os.replace(tmp_path, metadata_path)


def iter_processing_v1_result_metadata(
    storage_path: str | Path,
    known_node_signatures: dict[str, set[str]] | None = None,
) -> list[dict[str, Any]]:
    """Return ProcessingTool v1 result metadata, inferring old records when possible."""
    results_root = Path(storage_path) / "cache" / "v1" / "results"
    if not results_root.exists():
        return []
    known_node_signatures = known_node_signatures or {}
    rows: list[dict[str, Any]] = []
    for result_dir in results_root.glob("*/*/rk_*"):
        current_path = result_dir / "current.json"
        if not current_path.exists():
            continue
        metadata_path = result_dir / "result.json"
        if metadata_path.exists():
            try:
                metadata = json.loads(metadata_path.read_text())
            except (OSError, json.JSONDecodeError):
                metadata = {}
            if (
                metadata.get("schema") == "bioimageflow.cache.result.v1"
                and metadata.get("kind") == "processing_tool"
            ):
                rows.append(metadata)
                continue
        result_key = result_dir.name
        for node_name, signatures in known_node_signatures.items():
            for sig_hash in signatures:
                if processing_v1_result_key(node_name, sig_hash) == result_key:
                    rows.append(
                        {
                            "schema": "bioimageflow.cache.result.v1",
                            "kind": "processing_tool",
                            "node": node_name,
                            "signature_hash": sig_hash,
                            "result_key": result_key,
                        }
                    )
    return rows


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


def processing_v1_prepare_attempt(
    storage_path: str | Path,
    node_name: str,
    sig_hash: str,
) -> tuple[str, str, Path, Path]:
    """Create a v1 attempt staging tree for a ProcessingTool node."""
    storage = StorageV1(storage_path)
    result_key = processing_v1_result_key(node_name, sig_hash)
    attempt_id = storage.new_attempt_id()
    result_dir = storage.result_dir(result_key)
    result_dir.mkdir(parents=True, exist_ok=True)
    _ensure_existing_v1_dir(result_dir, storage.cache_root, "Result directory")
    attempts_dir = _ensure_v1_child_dir(result_dir, "attempts", "Attempts directory")
    attempt_dir = _ensure_v1_child_dir(attempts_dir, attempt_id, "Attempt directory")
    staging_dir = _ensure_v1_child_dir(attempt_dir, "staging", "Attempt staging directory")
    assets_dir = _ensure_v1_child_dir(staging_dir, "assets", "Attempt assets directory")
    _ensure_v1_child_dir(staging_dir, "work", "Attempt work directory")
    return result_key, attempt_id, staging_dir, assets_dir


def _ensure_existing_v1_dir(path: Path, parent: Path, label: str) -> None:
    try:
        path.resolve().relative_to(parent.resolve())
    except ValueError as exc:
        raise CacheCorruptionError(f"{label} escapes its parent directory.") from exc
    if path.is_symlink():
        raise CacheCorruptionError(f"{label} must not be a symlink.")
    if not path.is_dir():
        raise CacheCorruptionError(f"{label} must be a directory.")


def _ensure_v1_child_dir(parent: Path, name: str, label: str) -> Path:
    validate_relative_posix_path(name)
    path = parent / name
    if path.exists() or path.is_symlink():
        _ensure_existing_v1_dir(path, parent, label)
    else:
        path.mkdir()
    return path


def _ensure_v1_records_dir(result_dir: Path) -> Path:
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
    return records_dir


def _ensure_v1_record_dir(result_dir: Path, record_id: str) -> Path:
    records_dir = _ensure_v1_records_dir(result_dir)
    record_dir = records_dir / record_id
    if record_dir.exists() or record_dir.is_symlink():
        try:
            record_dir.resolve().relative_to(records_dir.resolve())
        except ValueError as exc:
            raise CacheCorruptionError("Record directory escapes records directory.") from exc
        if record_dir.is_symlink():
            raise CacheCorruptionError("Record directory must not be a symlink.")
    else:
        record_dir.mkdir(parents=True)
    return record_dir


def _rehydrate_processing_paths(
    df: pd.DataFrame,
    record_dir: Path,
    path_columns: set[str],
) -> pd.DataFrame:
    if not path_columns:
        return df
    hydrated = df.copy()
    for column in path_columns:
        if column not in hydrated.columns:
            continue

        def _rehydrate(value: Any) -> Any:
            if not isinstance(value, str) or not value.startswith("assets/"):
                return value
            path = record_dir / value
            try:
                path.resolve().relative_to(record_dir.resolve())
            except ValueError as exc:
                raise CacheCorruptionError("Cached asset path escapes record directory.") from exc
            return str(path)

        hydrated[column] = hydrated[column].map(_rehydrate)
    return hydrated


def _shared_array_manifest_by_path(outputs: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {
        str(output.get("path")): output
        for output in outputs
        if output.get("kind") == "owned_asset"
        and output.get("asset_role") == "shared_array"
    }


def _rehydrate_processing_shared_arrays(
    df: pd.DataFrame,
    record_dir: Path,
    shared_array_columns: set[str],
    outputs: list[dict[str, Any]],
) -> pd.DataFrame:
    if not shared_array_columns:
        return df
    from bioimageflow_core.shm import create_shared_output

    hydrated = df.copy()
    shared_outputs = _shared_array_manifest_by_path(outputs)
    for column in shared_array_columns:
        if column not in hydrated.columns:
            continue

        def _rehydrate(value: Any) -> Any:
            if not isinstance(value, str) or not value.startswith("assets/shm/"):
                return value
            try:
                relative = validate_relative_posix_path(value)
            except ValueError as exc:
                raise CacheCorruptionError("Cached shared-array asset path is unsafe.") from exc
            output = shared_outputs.get(relative)
            if output is None:
                raise CacheCorruptionError(f"Cached shared-array asset is missing manifest metadata: {relative}")
            array_metadata = output.get("array")
            if not isinstance(array_metadata, dict):
                raise CacheCorruptionError(f"Cached shared-array asset metadata is invalid: {relative}")
            path = record_dir / relative
            try:
                path.resolve().relative_to(record_dir.resolve())
            except ValueError as exc:
                raise CacheCorruptionError("Cached shared-array asset path escapes record directory.") from exc
            try:
                import numpy as np

                array = np.load(path, allow_pickle=False)
            except Exception as exc:
                raise CacheCorruptionError(f"Cached shared-array asset is unreadable: {relative}") from exc
            if list(array.shape) != list(array_metadata.get("shape", [])):
                raise CacheCorruptionError(f"Cached shared-array shape mismatch: {relative}")
            if str(array.dtype) != str(array_metadata.get("dtype", "")):
                raise CacheCorruptionError(f"Cached shared-array dtype mismatch: {relative}")
            if str(array_metadata.get("order", "")) != "C" or not array.flags.c_contiguous:
                raise CacheCorruptionError(f"Cached shared-array memory order mismatch: {relative}")
            with create_shared_output(array) as ref:
                return ref

        hydrated[column] = hydrated[column].map(_rehydrate)
    return hydrated


def _rehydrate_processing_assets(
    df: pd.DataFrame,
    record_dir: Path,
    path_columns: set[str],
    shared_array_columns: set[str],
    outputs: list[dict[str, Any]],
) -> pd.DataFrame:
    hydrated = _rehydrate_processing_shared_arrays(
        df,
        record_dir,
        shared_array_columns,
        outputs,
    )
    return _rehydrate_processing_paths(hydrated, record_dir, path_columns)


def processing_v1_lookup(
    storage_path: str | Path,
    node_name: str,
    sig_hash: str,
    path_columns: set[str],
    shared_array_columns: set[str] | None = None,
    hydrate_assets: bool = True,
) -> pd.DataFrame | None:
    """Load a ProcessingTool v1 cache hit, or return ``None`` on miss."""
    storage = StorageV1(storage_path)
    result_key = processing_v1_result_key(node_name, sig_hash)
    pointer = storage.load_current(result_key)
    if pointer is None:
        return None
    manifest = storage._load_record_manifest(result_key, pointer.record_id)
    record_dir = storage.result_dir(result_key) / "records" / pointer.record_id
    try:
        df = cache_load(record_dir / "dataframe.parquet")
    except Exception as exc:
        raise CacheCorruptionError("Cached v1 ProcessingTool dataframe is unreadable.") from exc
    if not hydrate_assets:
        return df
    return _rehydrate_processing_assets(
        df,
        record_dir,
        path_columns,
        shared_array_columns or set(),
        manifest.outputs,
    )


def _safe_asset_segment(value: Any) -> str:
    raw = str(value)
    sanitized = re.sub(r"[^A-Za-z0-9_.-]+", "_", raw).strip("._-") or "value"
    sanitized = sanitized[:48]
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:8]
    return f"{sanitized}_{digest}"


def _write_shared_array_asset(
    value: Any,
    *,
    column: str,
    row_index: Any,
    row_position: int,
    staging_assets_dir: Path,
) -> tuple[str, dict[str, Any], Path]:
    from bioimageflow_core.shm import open_shared_array
    from bioimageflow_core.types import SharedArray

    if not isinstance(value, SharedArray):
        raise CacheCorruptionError(f"Shared-array output column contains unsupported value: {type(value).__name__}")

    try:
        import numpy as np

        with open_shared_array(value) as source:
            array = np.array(source, copy=True, order="C")
    except Exception as exc:
        raise CacheCorruptionError(f"Shared-array output could not be opened for column {column!r}.") from exc

    column_segment = _safe_asset_segment(column)
    row_segment = _safe_asset_segment(row_index)
    relative = validate_relative_posix_path(
        f"assets/shm/{column_segment}/{row_position:06d}_{row_segment}.npy"
    )
    path = staging_assets_dir / "shm" / column_segment / f"{row_position:06d}_{row_segment}.npy"
    path.parent.mkdir(parents=True, exist_ok=True)
    np.save(path, array, allow_pickle=False)
    size, digest = asset_digest_and_size(path)
    entry = {
        "array": {
            "column": str(column),
            "dtype": str(array.dtype),
            "format": "npy",
            "order": "C",
            "row_index": str(row_index),
            "shape": list(array.shape),
        },
        "asset_role": "shared_array",
        "digest": digest,
        "kind": "owned_asset",
        "path": relative,
        "size": size,
    }
    return relative, entry, path


def _processing_manifest_entries_and_dataframe(
    df: pd.DataFrame,
    path_columns: set[str],
    owned_path_columns: set[str],
    staging_assets_dir: Path,
    shared_array_columns: set[str] | None = None,
) -> tuple[pd.DataFrame, list[dict[str, Any]], dict[str, Path]]:
    stored = df.copy()
    outputs: list[dict[str, Any]] = []
    owned_assets: dict[str, Path] = {}
    seen_outputs: set[tuple[str, str]] = set()
    staging_root = staging_assets_dir.resolve()
    shared_array_columns = shared_array_columns or set()
    from bioimageflow_core.types import SharedArray

    for column in shared_array_columns:
        if column not in stored.columns:
            continue
        for row_position, index in enumerate(stored.index):
            value = stored.at[index, column]
            if value is None or (isinstance(value, float) and pd.isna(value)):
                continue
            if not isinstance(value, SharedArray):
                if column in path_columns:
                    continue
                raise CacheCorruptionError(
                    f"Shared-array output column contains unsupported value: {type(value).__name__}"
                )
            record_relative, entry, path = _write_shared_array_asset(
                value,
                column=column,
                row_index=index,
                row_position=row_position,
                staging_assets_dir=staging_assets_dir,
            )
            if record_relative in owned_assets:
                raise CacheCorruptionError(f"Duplicate shared-array asset path: {record_relative}")
            owned_assets[record_relative] = path
            outputs.append(entry)
            seen_outputs.add(("owned_asset", record_relative))
            stored.at[index, column] = record_relative
    for column in path_columns:
        if column not in stored.columns:
            continue
        for index in stored.index:
            value = stored.at[index, column]
            if value is None or (isinstance(value, float) and pd.isna(value)):
                continue
            if column in shared_array_columns and isinstance(value, str) and value.startswith("assets/shm/"):
                continue
            if not isinstance(value, (str, os.PathLike)):
                raise CacheCorruptionError(
                    f"Declared path output column {column!r} contains unsupported value: {type(value).__name__}"
                )
            path = Path(value)
            if not path.is_absolute():
                path = Path.cwd() / path
            try:
                relative = path.resolve().relative_to(staging_root)
            except ValueError:
                if column in owned_path_columns:
                    raise CacheCorruptionError(
                        f"Declared owned output asset is outside staging assets: {path}"
                    )
                external = path.as_posix()
                entry_key = ("external_path", external)
                if entry_key not in seen_outputs:
                    outputs.append({"path": external, "kind": "external_path", "identity": "path"})
                    seen_outputs.add(entry_key)
                stored.at[index, column] = external
                continue
            try:
                record_relative = validate_relative_posix_path(f"assets/{relative.as_posix()}")
            except ValueError as exc:
                raise CacheCorruptionError("Declared output asset path is unsafe.") from exc
            if record_relative.startswith("assets/shm/"):
                raise CacheCorruptionError("assets/shm/ is reserved for shared-array assets.")
            if not path.exists():
                raise CacheCorruptionError(f"Declared output asset is missing: {path}")
            try:
                path.resolve().relative_to(staging_root)
            except ValueError as exc:
                raise CacheCorruptionError(f"Declared output asset escapes staging assets: {path}") from exc
            size, digest = asset_digest_and_size(path)
            previous = owned_assets.get(record_relative)
            if previous is not None and previous.resolve() != path.resolve():
                raise CacheCorruptionError(f"Duplicate owned asset path: {record_relative}")
            for existing_relative, existing_path in owned_assets.items():
                if existing_relative == record_relative:
                    continue
                if record_relative.startswith(f"{existing_relative}/") or existing_relative.startswith(f"{record_relative}/"):
                    raise CacheCorruptionError(
                        f"Overlapping owned asset paths are not supported: {existing_relative}, {record_relative}"
                    )
            owned_assets[record_relative] = path
            entry_key = ("owned_asset", record_relative)
            if entry_key not in seen_outputs:
                entry = {
                    "path": record_relative,
                    "kind": "owned_asset",
                    "size": size,
                    "digest": digest,
                }
                if path.is_dir():
                    entry["asset_type"] = "directory"
                outputs.append(entry)
                seen_outputs.add(entry_key)
            stored.at[index, column] = record_relative
    return stored, outputs, owned_assets


def processing_v1_publish(
    storage_path: str | Path,
    node_name: str,
    sig_hash: str,
    df: pd.DataFrame,
    *,
    result_key: str,
    attempt_id: str,
    staging_dir: Path,
    staging_assets_dir: Path,
    path_columns: set[str],
    owned_path_columns: set[str],
    shared_array_columns: set[str] | None = None,
) -> pd.DataFrame:
    """Publish a source ProcessingTool attempt as a v1 immutable record."""
    storage = StorageV1(storage_path)
    stored_df, outputs, owned_assets = _processing_manifest_entries_and_dataframe(
        df,
        path_columns,
        owned_path_columns,
        staging_assets_dir,
        shared_array_columns,
    )
    staging_parquet = staging_dir / "dataframe.parquet"
    _prepare_dataframe_for_parquet(stored_df).to_parquet(staging_parquet, index=True)
    dataframe_digest = _file_sha256(staging_parquet)
    manifest_material = {
        "schema": "bioimageflow.cache.record.v1",
        "result_key": result_key,
        "dataframe": {
            "path": "dataframe.parquet",
            "digest": dataframe_digest,
        },
        "outputs": outputs,
    }
    record_id = make_record_id(manifest_material)
    result_dir = storage.result_dir(result_key)
    _write_processing_v1_result_metadata(
        result_dir,
        node_name=node_name,
        sig_hash=sig_hash,
        result_key=result_key,
        attempt_id=attempt_id,
    )
    record_dir = _ensure_v1_record_dir(result_dir, record_id)
    for relative, source in owned_assets.items():
        parts = validate_relative_posix_path(relative).split("/")
        if parts[0] != "assets":
            raise CacheCorruptionError("Owned asset path must be under assets/.")
        destination_parent = record_dir
        for part in parts[:-1]:
            destination_parent = _ensure_v1_child_dir(
                destination_parent,
                part,
                "Record asset directory",
            )
        destination = destination_parent / parts[-1]
        if destination.exists() or destination.is_symlink():
            try:
                destination.resolve().relative_to(record_dir.resolve())
            except ValueError as exc:
                raise CacheCorruptionError("Owned asset path escapes record directory.") from exc
            if destination.is_symlink():
                raise CacheCorruptionError("Owned asset must not be a symlink.")
            if source.is_dir() != destination.is_dir():
                raise CacheCorruptionError("Owned asset path has incompatible type.")
            continue
        tmp_asset = destination_parent / f".{destination.name}.{attempt_id}.tmp"
        if source.is_dir():
            shutil.copytree(source, tmp_asset)
        else:
            shutil.copy2(source, tmp_asset)
        os.replace(tmp_asset, destination)
    record_parquet = record_dir / "dataframe.parquet"
    if not record_parquet.exists():
        tmp_parquet = record_dir / f".dataframe.{attempt_id}.tmp"
        shutil.copy2(staging_parquet, tmp_parquet)
        os.replace(tmp_parquet, record_parquet)
    manifest = RecordManifest(
        result_key=result_key,
        record_id=record_id,
        dataframe_digest=dataframe_digest,
        outputs=outputs,
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
        run_id=f"run_{attempt_id}",
    )
    selected_record_dir = storage.result_dir(result_key) / "records" / pointer.record_id
    selected_manifest = storage._load_record_manifest(result_key, pointer.record_id)
    try:
        selected_df = cache_load(selected_record_dir / "dataframe.parquet")
    except Exception as exc:
        raise CacheCorruptionError("Published v1 ProcessingTool dataframe is unreadable.") from exc
    return _rehydrate_processing_assets(
        selected_df,
        selected_record_dir,
        path_columns,
        shared_array_columns or set(),
        selected_manifest.outputs,
    )
