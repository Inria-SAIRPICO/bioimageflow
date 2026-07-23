"""Focused cache operations for dataframe."""

from __future__ import annotations

from .common import (
    CacheCorruptionError,
    Path,
    RecordManifest,
    Storage,
    json,
    make_record_id,
    os,
    pd,
    shutil,
)
from .identity import (
    dataframe_result_key,
)
from .metadata import (
    _file_sha256,
    _prepare_dataframe_for_parquet,
    _write_dataframe_result_metadata,
    cache_load,
)


def _dataframe_record_path(storage: Storage, result_key: str, record_id: str) -> Path:
    return storage.result_dir(result_key) / "records" / record_id / "dataframe.parquet"


def dataframe_lookup(
    storage_path: str | Path,
    node_name: str,
    sig_hash: str,
) -> pd.DataFrame | None:
    """Load a DataFrameTool cache hit, or return ``None`` on miss."""
    storage = Storage(storage_path)
    result_key = dataframe_result_key(node_name, sig_hash)
    pointer = storage.load_current(result_key)
    if pointer is None:
        return None
    try:
        return cache_load(
            _dataframe_record_path(storage, result_key, pointer.record_id)
        )
    except Exception as exc:
        raise CacheCorruptionError("Cached dataframe is unreadable.") from exc


def dataframe_publish(
    storage_path: str | Path,
    node_name: str,
    sig_hash: str,
    df: pd.DataFrame,
) -> pd.DataFrame:
    """Publish a DataFrameTool result through the immutable record model."""
    storage = Storage(storage_path)
    result_key = dataframe_result_key(node_name, sig_hash)
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
            raise CacheCorruptionError(
                "Records directory escapes result directory."
            ) from exc
        if records_dir.is_symlink():
            raise CacheCorruptionError("Records directory must not be a symlink.")
    else:
        records_dir.mkdir(parents=True)
    record_dir = records_dir / record_id
    if record_dir.exists() or record_dir.is_symlink():
        try:
            record_dir.resolve().relative_to((result_dir / "records").resolve())
        except ValueError as exc:
            raise CacheCorruptionError(
                "Record directory escapes records directory."
            ) from exc
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
        tmp_manifest.write_text(
            json.dumps(manifest.to_dict(), indent=2, sort_keys=True)
        )
        os.replace(tmp_manifest, manifest_path)
    _write_dataframe_result_metadata(
        result_dir,
        node_name=node_name,
        sig_hash=sig_hash,
        result_key=result_key,
        attempt_id=attempt_id,
    )
    pointer = storage.select_current_record(
        result_key,
        candidate_record_id=record_id,
        attempt_id=attempt_id,
        run_id=run_id,
    )
    try:
        return cache_load(
            _dataframe_record_path(storage, result_key, pointer.record_id)
        )
    except Exception as exc:
        raise CacheCorruptionError("Published dataframe is unreadable.") from exc
