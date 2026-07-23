"""Focused tests split from ``tests/unit/test_storage.py``."""

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


from tests.testkit.storage import (
    _file_digest,
    _record_id_for,
)


def test_record_manifest_validation_checks_files_and_digest(tmp_path: Path) -> None:
    result_key = make_result_key({"node": "segment"})
    dataframe_digest = _file_digest(b"parquet")
    output = {
        "path": "assets/mask.tif",
        "kind": "owned_asset",
        "size": 4,
        "digest": _file_digest(b"not-mask"),
    }
    record_id = _record_id_for(result_key, dataframe_digest, [output])
    record_dir = tmp_path / "records" / record_id
    record_dir.mkdir(parents=True)
    (record_dir / "dataframe.parquet").write_bytes(b"parquet")
    asset = record_dir / "assets" / "mask.tif"
    asset.parent.mkdir()
    asset.write_bytes(b"mask")

    manifest = RecordManifest(
        result_key=result_key,
        record_id=record_id,
        dataframe_digest=dataframe_digest,
        outputs=[output],
    )

    with pytest.raises(CacheCorruptionError, match="digest"):
        manifest.validate(record_dir)

    valid_output = {
        "path": "assets/mask.tif",
        "kind": "owned_asset",
        "size": 4,
        "digest": _file_digest(b"mask"),
    }
    record_id = _record_id_for(result_key, dataframe_digest, [valid_output])
    record_dir = tmp_path / "records" / record_id
    record_dir.mkdir(parents=True)
    (record_dir / "dataframe.parquet").write_bytes(b"parquet")
    asset = record_dir / "assets" / "mask.tif"
    asset.parent.mkdir()
    asset.write_bytes(b"mask")
    manifest = RecordManifest(
        result_key=result_key,
        record_id=record_id,
        dataframe_digest=dataframe_digest,
        outputs=[valid_output],
    )
    manifest.validate(record_dir)

    bad_record_id = _record_id_for(
        result_key,
        dataframe_digest,
        [{"path": "../escape.tif", "kind": "owned_asset", "size": 1}],
    )
    bad_record_dir = tmp_path / "records" / bad_record_id
    bad_record_dir.mkdir(parents=True)
    (bad_record_dir / "dataframe.parquet").write_bytes(b"parquet")
    bad_manifest = RecordManifest(
        result_key=result_key,
        record_id=bad_record_id,
        dataframe_digest=dataframe_digest,
        outputs=[{"path": "../escape.tif", "kind": "owned_asset", "size": 1}],
    )
    with pytest.raises(CacheCorruptionError):
        bad_manifest.validate(bad_record_dir)


def test_record_manifest_validation_accepts_scalar_outputs(tmp_path: Path) -> None:
    result_key = make_result_key({"node": "hotspot"})
    dataframe_digest = _file_digest(b"parquet")
    output = {
        "kind": "scalar_output",
        "output_column": "spot_count",
        "row_index": "0",
        "value": {"kind": "signed_integer", "value": "0"},
    }
    record_id = _record_id_for(result_key, dataframe_digest, [output])
    record_dir = tmp_path / "records" / record_id
    record_dir.mkdir(parents=True)
    (record_dir / "dataframe.parquet").write_bytes(b"parquet")

    manifest = RecordManifest(
        result_key=result_key,
        record_id=record_id,
        dataframe_digest=dataframe_digest,
        outputs=[output],
    )

    manifest.validate(record_dir)
    assert manifest.to_dict()["outputs"] == [output]


@pytest.mark.parametrize(
    "output",
    [
        {
            "kind": "scalar_output",
            "output_column": "",
            "row_index": "0",
            "value": {"kind": "signed_integer", "value": "0"},
        },
        {
            "kind": "scalar_output",
            "output_column": "spot_count",
            "row_index": "",
            "value": {"kind": "signed_integer", "value": "0"},
        },
        {
            "kind": "scalar_output",
            "output_column": "spot_count",
            "row_index": "0",
            "value": 0,
        },
        {
            "kind": "scalar_output",
            "output_column": "spot_count",
            "row_index": "0",
            "value": {"kind": "external_path", "value": "/tmp/a"},
        },
        {
            "kind": "scalar_output",
            "output_column": "spot_count",
            "row_index": "0",
            "value": {"kind": "signed_integer", "value": "1.5"},
        },
    ],
)
def test_record_manifest_validation_rejects_invalid_scalar_outputs(
    tmp_path: Path,
    output: dict[str, object],
) -> None:
    result_key = make_result_key({"node": "hotspot"})
    dataframe_digest = _file_digest(b"parquet")
    record_id = _record_id_for(result_key, dataframe_digest, [output])
    record_dir = tmp_path / "records" / record_id
    record_dir.mkdir(parents=True)
    (record_dir / "dataframe.parquet").write_bytes(b"parquet")
    manifest = RecordManifest(
        result_key=result_key,
        record_id=record_id,
        dataframe_digest=dataframe_digest,
        outputs=[output],
    )

    with pytest.raises(CacheCorruptionError):
        manifest.validate(record_dir)


@pytest.mark.parametrize(
    ("escape_case", "expected_match"),
    [
        pytest.param("asset", "escapes", id="owned-asset"),
        pytest.param("dataframe", "dataframe", id="dataframe"),
    ],
)
def test_record_manifest_validation_rejects_symlink_escape(
    tmp_path: Path,
    escape_case: str,
    expected_match: str,
) -> None:
    result_key = make_result_key({"node": "segment"})
    dataframe_digest = _file_digest(b"parquet")
    outputs: list[dict[str, object]] = []
    if escape_case == "asset":
        outputs = [
            {
                "path": "assets/mask.tif",
                "kind": "owned_asset",
                "size": 4,
                "digest": _file_digest(b"mask"),
            }
        ]

    record_id = _record_id_for(result_key, dataframe_digest, outputs)
    record_dir = tmp_path / "records" / record_id
    record_dir.mkdir(parents=True)
    if escape_case == "asset":
        (record_dir / "dataframe.parquet").write_bytes(b"parquet")
        outside = tmp_path / "outside.tif"
        outside.write_bytes(b"mask")
        asset = record_dir / "assets" / "mask.tif"
        asset.parent.mkdir()
        asset.symlink_to(outside)
    else:
        outside = tmp_path / "outside.parquet"
        outside.write_bytes(b"parquet")
        (record_dir / "dataframe.parquet").symlink_to(outside)

    manifest = RecordManifest(
        result_key=result_key,
        record_id=record_id,
        dataframe_digest=dataframe_digest,
        outputs=outputs,
    )

    with pytest.raises(CacheCorruptionError, match=expected_match):
        manifest.validate(record_dir)


def test_record_manifest_validation_checks_declared_dataframe_file_digest(
    tmp_path: Path,
) -> None:
    result_key = make_result_key({"node": "segment"})
    dataframe_digest = _file_digest(b"expected")
    record_id = _record_id_for(result_key, dataframe_digest, [])
    record_dir = tmp_path / "records" / record_id
    record_dir.mkdir(parents=True)
    (record_dir / "dataframe.parquet").write_bytes(b"actual")
    manifest = RecordManifest(
        result_key=result_key,
        record_id=record_id,
        dataframe_digest=dataframe_digest,
        outputs=[],
    )

    with pytest.raises(CacheCorruptionError, match="digest"):
        manifest.validate(record_dir)


def test_record_manifest_from_dict_rejects_non_mapping_and_unknown_fields() -> None:
    result_key = make_result_key({"node": "segment"})
    manifest = {
        "schema": "bioimageflow.cache.record.v1",
        "result_key": result_key,
        "record_id": _record_id_for(result_key, "sha256:" + "1" * 64, []),
        "dataframe": {"path": "dataframe.parquet", "digest": "sha256:" + "1" * 64},
        "outputs": [],
        "extra": "not allowed",
    }

    with pytest.raises(CacheCorruptionError):
        RecordManifest.from_dict([])
    with pytest.raises(CacheCorruptionError):
        RecordManifest.from_dict(manifest)


def test_make_record_id_excludes_execution_metadata() -> None:
    manifest = {
        "result_key": "rk_abcdef",
        "dataframe_digest": "sha256:" + "1" * 64,
        "outputs": [{"path": "assets/a.tif", "kind": "owned_asset", "size": 1}],
        "attempt_id": "attempt_a",
        "created_at": "today",
    }

    other = dict(manifest, attempt_id="attempt_b", created_at="tomorrow")

    assert make_record_id(manifest) == make_record_id(other)
