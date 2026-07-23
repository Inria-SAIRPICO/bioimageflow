"""Focused cache operations for processing lookup."""

from __future__ import annotations

from .common import (
    Any,
    CacheCorruptionError,
    Path,
    Storage,
    pd,
    validate_relative_posix_path,
)
from .identity import (
    processing_result_key,
)
from .metadata import (
    cache_load,
)


def processing_prepare_attempt(
    storage_path: str | Path,
    node_name: str,
    sig_hash: str,
    *,
    run_id: str,
    invocation_id: str,
    engine: str,
    tool_identity: str,
) -> tuple[str, str, Path, Path]:
    """Create an attempt staging tree for a ProcessingTool node."""
    storage = Storage(storage_path)
    result_key = processing_result_key(node_name, sig_hash)
    attempt_id = storage.new_attempt_id()
    result_dir = storage.result_dir(result_key)
    result_dir.mkdir(parents=True, exist_ok=True)
    _ensure_existing_storage_dir(result_dir, storage.cache_root, "Result directory")
    attempts_dir = _ensure_record_child_dir(
        result_dir, "attempts", "Attempts directory"
    )
    attempt_dir = _ensure_record_child_dir(
        attempts_dir, attempt_id, "Attempt directory"
    )
    staging_dir = _ensure_record_child_dir(
        attempt_dir, "staging", "Attempt staging directory"
    )
    assets_dir = _ensure_record_child_dir(
        staging_dir, "assets", "Attempt assets directory"
    )
    _ensure_record_child_dir(staging_dir, "work", "Attempt work directory")
    storage.start_cache_attempt(
        result_key,
        attempt_id,
        run_id=run_id,
        node_key=node_name,
        invocation_id=invocation_id,
        tool_identity=tool_identity,
        engine=engine,
    )
    return result_key, attempt_id, staging_dir, assets_dir


def _ensure_existing_storage_dir(path: Path, parent: Path, label: str) -> None:
    try:
        path.resolve().relative_to(parent.resolve())
    except ValueError as exc:
        raise CacheCorruptionError(f"{label} escapes its parent directory.") from exc
    if path.is_symlink():
        raise CacheCorruptionError(f"{label} must not be a symlink.")
    if not path.is_dir():
        raise CacheCorruptionError(f"{label} must be a directory.")


def _ensure_record_child_dir(parent: Path, name: str, label: str) -> Path:
    validate_relative_posix_path(name)
    path = parent / name
    if path.exists() or path.is_symlink():
        _ensure_existing_storage_dir(path, parent, label)
    else:
        path.mkdir()
    return path


def _ensure_records_dir(result_dir: Path) -> Path:
    records_dir = result_dir / "records"
    records_dir.mkdir(parents=True, exist_ok=True)
    try:
        records_dir.resolve().relative_to(result_dir.resolve())
    except ValueError as exc:
        raise CacheCorruptionError(
            "Records directory escapes result directory."
        ) from exc
    if records_dir.is_symlink():
        raise CacheCorruptionError("Records directory must not be a symlink.")
    return records_dir


def _ensure_record_dir(result_dir: Path, record_id: str) -> Path:
    records_dir = _ensure_records_dir(result_dir)
    record_dir = records_dir / record_id
    if record_dir.exists() or record_dir.is_symlink():
        try:
            record_dir.resolve().relative_to(records_dir.resolve())
        except ValueError as exc:
            raise CacheCorruptionError(
                "Record directory escapes records directory."
            ) from exc
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
                raise CacheCorruptionError(
                    "Cached asset path escapes record directory."
                ) from exc
            return str(path)

        hydrated[column] = hydrated[column].map(_rehydrate)
    return hydrated


def _shared_array_manifest_by_path(
    outputs: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
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
                raise CacheCorruptionError(
                    "Cached shared-array asset path is unsafe."
                ) from exc
            output = shared_outputs.get(relative)
            if output is None:
                raise CacheCorruptionError(
                    f"Cached shared-array asset is missing manifest metadata: {relative}"
                )
            array_metadata = output.get("array")
            if not isinstance(array_metadata, dict):
                raise CacheCorruptionError(
                    f"Cached shared-array asset metadata is invalid: {relative}"
                )
            path = record_dir / relative
            try:
                path.resolve().relative_to(record_dir.resolve())
            except ValueError as exc:
                raise CacheCorruptionError(
                    "Cached shared-array asset path escapes record directory."
                ) from exc
            try:
                import numpy as np

                array = np.load(path, allow_pickle=False)
            except Exception as exc:
                raise CacheCorruptionError(
                    f"Cached shared-array asset is unreadable: {relative}"
                ) from exc
            if list(array.shape) != list(array_metadata.get("shape", [])):
                raise CacheCorruptionError(
                    f"Cached shared-array shape mismatch: {relative}"
                )
            if str(array.dtype) != str(array_metadata.get("dtype", "")):
                raise CacheCorruptionError(
                    f"Cached shared-array dtype mismatch: {relative}"
                )
            if (
                str(array_metadata.get("order", "")) != "C"
                or not array.flags.c_contiguous
            ):
                raise CacheCorruptionError(
                    f"Cached shared-array memory order mismatch: {relative}"
                )
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


def processing_lookup(
    storage_path: str | Path,
    node_name: str,
    sig_hash: str,
    path_columns: set[str],
    shared_array_columns: set[str] | None = None,
    hydrate_assets: bool = True,
) -> pd.DataFrame | None:
    """Load a ProcessingTool cache hit, or return ``None`` on miss."""
    storage = Storage(storage_path)
    result_key = processing_result_key(node_name, sig_hash)
    pointer = storage.load_current(result_key)
    if pointer is None:
        return None
    manifest = storage._load_record_manifest(result_key, pointer.record_id)
    record_dir = storage.result_dir(result_key) / "records" / pointer.record_id
    try:
        df = cache_load(record_dir / "dataframe.parquet")
    except Exception as exc:
        raise CacheCorruptionError(
            "Cached ProcessingTool dataframe is unreadable."
        ) from exc
    if not hydrate_assets:
        return df
    return _rehydrate_processing_assets(
        df,
        record_dir,
        path_columns,
        shared_array_columns or set(),
        manifest.outputs,
    )
