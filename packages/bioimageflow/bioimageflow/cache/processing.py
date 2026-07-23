"""Focused cache operations for processing."""

from __future__ import annotations

from .common import (
    Any,
    CacheCorruptionError,
    Iterable,
    Path,
    RecordManifest,
    Storage,
    json,
    make_record_id,
    os,
    pd,
    shutil,
    validate_relative_posix_path,
)
from .metadata import (
    _file_sha256,
    _prepare_dataframe_for_parquet,
    _write_processing_result_metadata,
    cache_load,
)
from .processing_lookup import (
    _ensure_record_child_dir,
    _ensure_record_dir,
    _rehydrate_processing_assets,
)
from .assets import (
    _processing_manifest_entries_and_dataframe,
)


def processing_publish(
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
    declared_owned_artifact_paths: Iterable[tuple[str, Any, str | os.PathLike[str]]]
    | None = None,
    declared_scalar_outputs: Iterable[tuple[str, Any, Any]] | None = None,
) -> pd.DataFrame:
    """Publish a source ProcessingTool attempt as an immutable record."""
    storage = Storage(storage_path)
    stored_df, outputs, owned_assets = _processing_manifest_entries_and_dataframe(
        df,
        path_columns,
        owned_path_columns,
        staging_assets_dir,
        shared_array_columns,
        declared_owned_artifact_paths,
        declared_scalar_outputs,
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
    _write_processing_result_metadata(
        result_dir,
        node_name=node_name,
        sig_hash=sig_hash,
        result_key=result_key,
        attempt_id=attempt_id,
    )
    record_dir = _ensure_record_dir(result_dir, record_id)
    for relative, source in owned_assets.items():
        parts = validate_relative_posix_path(relative).split("/")
        if parts[0] != "assets":
            raise CacheCorruptionError("Owned asset path must be under assets/.")
        destination_parent = record_dir
        for part in parts[:-1]:
            destination_parent = _ensure_record_child_dir(
                destination_parent,
                part,
                "Record asset directory",
            )
        destination = destination_parent / parts[-1]
        if destination.exists() or destination.is_symlink():
            try:
                destination.resolve().relative_to(record_dir.resolve())
            except ValueError as exc:
                raise CacheCorruptionError(
                    "Owned asset path escapes record directory."
                ) from exc
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
        tmp_manifest.write_text(
            json.dumps(manifest.to_dict(), indent=2, sort_keys=True)
        )
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
        raise CacheCorruptionError(
            "Published ProcessingTool dataframe is unreadable."
        ) from exc
    return _rehydrate_processing_assets(
        selected_df,
        selected_record_dir,
        path_columns,
        shared_array_columns or set(),
        selected_manifest.outputs,
    )
