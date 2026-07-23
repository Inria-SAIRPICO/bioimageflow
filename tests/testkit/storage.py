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
    canonical_json_bytes,
    canonical_scalar_payload,
    make_node_keys,
    make_result_key,
    make_record_id,
    result_shard_parts,
    validate_relative_posix_path,
)


def _file_digest(content: bytes) -> str:
    return f"sha256:{hashlib.sha256(content).hexdigest()}"


def _record_id_for(
    result_key: str, dataframe_digest: str, outputs: list[dict[str, object]]
) -> str:
    return make_record_id(
        {
            "schema": "bioimageflow.cache.record.v1",
            "result_key": result_key,
            "dataframe": {
                "path": "dataframe.parquet",
                "digest": dataframe_digest,
            },
            "outputs": outputs,
        }
    )


def _write_record(
    storage: Storage,
    result_key: str,
    *,
    dataframe_digest: str | None = None,
    outputs: list[dict[str, object]] | None = None,
    dataframe_content: bytes = b"parquet",
) -> str:
    outputs = list(outputs or [])
    dataframe_digest = dataframe_digest or _file_digest(dataframe_content)
    record_id = _record_id_for(result_key, dataframe_digest, outputs)
    record_dir = storage.result_dir(result_key) / "records" / record_id
    record_dir.mkdir(parents=True)
    (record_dir / "dataframe.parquet").write_bytes(dataframe_content)
    for output in outputs:
        if output.get("kind") == "owned_asset":
            asset_path = record_dir / str(output["path"])
            asset_path.parent.mkdir(parents=True, exist_ok=True)
            asset_path.write_bytes(b"mask")
    manifest = RecordManifest(
        result_key=result_key,
        record_id=record_id,
        dataframe_digest=dataframe_digest,
        outputs=outputs,
    )
    (record_dir / "manifest.json").write_text(
        json.dumps(manifest.to_dict(), indent=2, sort_keys=True)
    )
    return record_id
