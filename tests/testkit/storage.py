"""Shared helpers for the focused tests split from ``tests/unit/test_storage.py``."""

from __future__ import annotations


# ruff: noqa: F401

import hashlib

import json

import os

from pathlib import Path

import pandas as pd

import pytest

from bioimageflow.storage import (
    CacheCorruptionError,
    CurrentPointer,
    OutputViewCapability,
    RecordManifest,
    Storage,
    canonical_dataframe_digest,
    canonical_dataframe_identity,
    canonical_json_bytes,
    canonical_scalar_payload,
    make_node_keys,
    make_result_key,
    make_record_id,
    result_shard_parts,
    validate_relative_posix_path,
)
from bioimageflow.cache import _write_canonical_parquet


def _file_digest(content: bytes) -> str:
    return f"sha256:{hashlib.sha256(content).hexdigest()}"


def _record_id_for(
    result_key: str,
    dataframe_digest: str,
    outputs: list[dict[str, object]],
    *,
    logical_schema: list[dict[str, object]] | None = None,
) -> str:
    return make_record_id(
        {
            "schema": "bioimageflow.cache.record.v1",
            "result_key": result_key,
            "dataframe": {
                "path": "dataframe.parquet",
                "format": "parquet",
                "logical_digest": dataframe_digest,
                "logical_schema": list(logical_schema or []),
                "transport_digest": "sha256:" + "0" * 64,
            },
            "outputs": outputs,
        }
    )


def _write_test_dataframe(
    path: Path,
    content: bytes = b"parquet",
) -> tuple[list[dict[str, object]], str, str]:
    frame = pd.DataFrame({"payload": [content.hex()]}, index=["row"])
    _write_canonical_parquet(frame, path)
    logical_schema, logical_digest = canonical_dataframe_identity(frame)
    transport_digest = _file_digest(path.read_bytes())
    return logical_schema, logical_digest, transport_digest


def _write_record(
    storage: Storage,
    result_key: str,
    *,
    dataframe_digest: str | None = None,
    outputs: list[dict[str, object]] | None = None,
    dataframe_content: bytes = b"parquet",
) -> str:
    outputs = [dict(output) for output in (outputs or [])]
    for output in outputs:
        if output.get("kind") == "owned_asset":
            output.setdefault("asset_type", "file")
    provisional_dir = storage.result_dir(result_key) / "records" / "provisional"
    provisional_dir.mkdir(parents=True)
    parquet_path = provisional_dir / "dataframe.parquet"
    logical_schema, logical_digest, transport_digest = _write_test_dataframe(
        parquet_path,
        dataframe_content,
    )
    logical_digest = dataframe_digest or logical_digest
    record_id = _record_id_for(
        result_key,
        logical_digest,
        outputs,
        logical_schema=logical_schema,
    )
    record_dir = storage.result_dir(result_key) / "records" / record_id
    provisional_dir.rename(record_dir)
    for output in outputs:
        if output.get("kind") == "owned_asset":
            asset_path = record_dir / str(output["path"])
            if output.get("asset_type") == "directory":
                asset_path.mkdir(parents=True, exist_ok=True)
                (asset_path / "content.txt").write_bytes(b"mask")
            else:
                asset_path.parent.mkdir(parents=True, exist_ok=True)
                asset_path.write_bytes(b"mask")
    manifest = RecordManifest(
        result_key=result_key,
        record_id=record_id,
        dataframe_logical_digest=logical_digest,
        dataframe_transport_digest=transport_digest,
        dataframe_logical_schema=logical_schema,
        outputs=outputs,
    )
    (record_dir / "manifest.json").write_text(
        json.dumps(manifest.to_dict(), indent=2, sort_keys=True)
    )
    return record_id
