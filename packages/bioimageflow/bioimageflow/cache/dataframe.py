"""Focused cache operations for dataframe."""

from __future__ import annotations

from .common import (
    CacheCorruptionError,
    Path,
    RecordManifest,
    Storage,
    canonical_dataframe_identity,
    json,
    make_record_id,
    pd,
)
from .identity import (
    dataframe_result_key,
)
from .metadata import (
    _file_sha256,
    _write_canonical_parquet,
    _write_dataframe_result_metadata,
    cache_load,
)
from .publication import create_record_candidate, install_record_candidate


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
    *,
    run_id: str,
    engine: str,
    tool_identity: str,
    column_kinds: dict[str, str] | None = None,
) -> pd.DataFrame:
    """Publish a DataFrameTool result through the immutable record model."""
    storage = Storage(storage_path)
    result_key = dataframe_result_key(node_name, sig_hash)
    attempt_id = storage.new_attempt_id()
    result_dir = storage.result_dir(result_key)
    staging_dir = result_dir / "attempts" / attempt_id / "staging"
    staging_dir.mkdir(parents=True, exist_ok=True)
    storage.start_cache_attempt(
        result_key,
        attempt_id,
        run_id=run_id,
        node_key=node_name,
        tool_identity=tool_identity,
        engine=engine,
    )
    staging_parquet = staging_dir / "dataframe.parquet"
    _write_canonical_parquet(df, staging_parquet)
    logical_schema, logical_digest = canonical_dataframe_identity(
        df,
        column_kinds=column_kinds,
    )
    transport_digest = _file_sha256(staging_parquet)
    manifest_material = {
        "schema": "bioimageflow.cache.record.v1",
        "result_key": result_key,
        "dataframe": {
            "path": "dataframe.parquet",
            "format": "parquet",
            "logical_digest": logical_digest,
            "logical_schema": logical_schema,
            "transport_digest": transport_digest,
        },
        "outputs": [],
    }
    record_id = make_record_id(manifest_material)
    candidate = create_record_candidate(
        storage,
        result_key,
        attempt_id,
        record_id,
    )
    (candidate / "dataframe.parquet").write_bytes(staging_parquet.read_bytes())
    manifest = RecordManifest(
        result_key=result_key,
        record_id=record_id,
        dataframe_logical_digest=logical_digest,
        dataframe_transport_digest=transport_digest,
        dataframe_logical_schema=logical_schema,
        outputs=[],
    )
    (candidate / "manifest.json").write_text(
        json.dumps(manifest.to_dict(), indent=2, sort_keys=True)
    )
    install_record_candidate(storage, result_key, record_id, candidate)
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
        selected = cache_load(
            _dataframe_record_path(storage, result_key, pointer.record_id)
        )
    except Exception as exc:
        raise CacheCorruptionError("Published dataframe is unreadable.") from exc
    storage.finish_cache_attempt(
        result_key,
        attempt_id,
        status="succeeded",
    )
    return selected
