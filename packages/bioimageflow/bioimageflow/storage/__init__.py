"""Cache storage primitives and public exports."""

# ruff: noqa: F401

from pathlib import Path
from typing import Literal

from .models import CacheCorruptionError, OutputViewCapability
from .manifests import CurrentPointer, RecordManifest
from .identity import (
    _atomic_write_json,
    _bioimageflow_version,
    _cell_payload,
    _datetime_payload,
    _file_sha256,
    _is_missing,
    _ordered_dataframe_columns,
    _safe_segment,
    _sha256_token,
    _validate_node_key,
    _validate_output_view_mode,
    _validate_path_segment,
    _validate_record_id,
    _validate_sha256_digest,
    asset_digest_and_size,
    canonical_dataframe_digest,
    canonical_dataframe_identity,
    canonical_json_bytes,
    canonical_scalar_payload,
    make_node_keys,
    make_record_id,
    make_result_key,
    result_shard_parts,
    validate_relative_posix_path,
)
from .storage import Storage


def export_outputs(
    storage_path: str | Path,
    *,
    mode: Literal["pointer", "symlink", "copy", "hardlink"] = "copy",
    scope: Literal["latest", "runs", "both"] = "latest",
    run_id: str | None = None,
) -> list[Path]:
    """Materialize assets, dataframes, and provenance from portable run views."""
    if scope not in {"latest", "runs", "both"}:
        raise ValueError(
            "Invalid output scope. Expected 'latest', 'runs', or 'both'."
        )
    storage = Storage(storage_path)
    materialized: list[Path] = []
    if scope in {"latest", "both"}:
        materialized.extend(storage.materialize_latest_outputs(mode))
    if scope in {"runs", "both"}:
        selected_run_id = run_id or storage.latest_success_run_id()
        if selected_run_id is None:
            raise CacheCorruptionError(
                "No successful run view is available for output export."
            )
        materialized.extend(storage.materialize_run_outputs(selected_run_id, mode))
    return materialized

__all__ = [
    "CacheCorruptionError",
    "CurrentPointer",
    "OutputViewCapability",
    "RecordManifest",
    "Storage",
    "asset_digest_and_size",
    "canonical_dataframe_digest",
    "canonical_dataframe_identity",
    "canonical_json_bytes",
    "canonical_scalar_payload",
    "export_outputs",
    "make_node_keys",
    "make_record_id",
    "make_result_key",
    "result_shard_parts",
    "validate_relative_posix_path",
]
