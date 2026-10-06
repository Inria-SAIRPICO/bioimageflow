"""Focused cache operations for dataframe."""

from __future__ import annotations

from typing import Any
import shutil

from bioimageflow.record_shared_assets import RecordSharedAssets
from bioimageflow.execution_state import CleanupRecorder

from bioimageflow.row_relation import ResultRelation, RowAssociation

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
)
from .publication import create_record_candidate, install_record_candidate
from .assets import native_array_assets
from .selection import SelectedResult, selected_result
from .lifecycle import AttemptFailure


def dataframe_lookup(
    storage_path: str | Path,
    node_name: str,
    sig_hash: str,
) -> SelectedResult | None:
    """Bind one exact DataFrameTool cache selection, or return ``None`` on miss."""
    storage = Storage(storage_path)
    result_key = dataframe_result_key(node_name, sig_hash)
    pointer = storage.load_current(result_key)
    if pointer is None:
        return None
    try:
        return selected_result(storage, result_key, pointer.record_id, hydrate_assets=True)
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
    row_relation: dict[str, Any] | None = None,
    run_context: CleanupRecorder | None = None,
) -> SelectedResult:
    """Publish a candidate and bind the actual first-valid selected winner."""
    storage = Storage(storage_path)
    result_key = dataframe_result_key(node_name, sig_hash)
    relation = (
        ResultRelation.from_dict(row_relation)
        if row_relation is not None
        else ResultRelation(
            "dataframe", f"source::{node_name}::{result_key}", "source",
            (RowAssociation((), tuple(str(index) for index in df.index)),),
        )
    ).to_dict()
    represented = tuple(index for group in relation["groups"] for index in group["output_indices"])
    if represented != tuple(str(index) for index in df.index):
        raise ValueError("Row relation output indices must match the dataframe index before publication.")
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
    failure = AttemptFailure.capture(storage, result_key, attempt_id)
    with RecordSharedAssets() as accepted_shared_assets:
        try:
            stored, outputs, owned_assets, native_kinds = native_array_assets(
                df, staging_dir / "assets", accepted_shared_assets)
            column_kinds = {**(column_kinds or {}), **native_kinds}
            staging_parquet = staging_dir / "dataframe.parquet"
            _write_canonical_parquet(stored, staging_parquet)
            logical_schema, logical_digest = canonical_dataframe_identity(
                stored,
                column_kinds=column_kinds,
            )
            transport_digest = _file_sha256(staging_parquet)
            manifest_material = {
                "schema": "bioimageflow.cache.record.v2",
                "result_key": result_key,
                "dataframe": {
                    "path": "dataframe.parquet",
                    "format": "parquet",
                    "logical_digest": logical_digest,
                    "logical_schema": logical_schema,
                    "transport_digest": transport_digest,
                },
                "outputs": outputs,
                "row_relation": relation,
            }
            record_id = make_record_id(manifest_material)
            candidate = create_record_candidate(
                storage,
                result_key,
                attempt_id,
                record_id,
            )
            for relative, path in owned_assets.items():
                destination = candidate / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, destination)
            shutil.copyfile(staging_parquet, candidate / "dataframe.parquet")
            manifest = RecordManifest(
                result_key=result_key,
                record_id=record_id,
                dataframe_logical_digest=logical_digest,
                dataframe_transport_digest=transport_digest,
                dataframe_logical_schema=logical_schema,
                outputs=outputs,
                row_relation=relation,
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
                selected = selected_result(storage, result_key, pointer.record_id, hydrate_assets=True,
                    accepted_shared_assets=accepted_shared_assets if pointer.record_id == record_id else None)
            except Exception as exc:
                raise CacheCorruptionError("Published dataframe is unreadable.") from exc
            storage.finish_cache_attempt(
                result_key,
                attempt_id,
                status="succeeded",
            )
            return selected
        except BaseException as primary:
            failure.finish(primary, run_context)
            raise
