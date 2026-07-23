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
    _write_record,
)


def test_run_metadata_and_node_result_view_write_selected_record(
    tmp_path: Path,
) -> None:
    storage = Storage(tmp_path)
    result_key = make_result_key({"node": "segment"})
    asset_output = {
        "path": "assets/mask.tif",
        "kind": "owned_asset",
        "size": 4,
        "digest": _file_digest(b"mask"),
        "asset_type": "file",
    }
    record_id = _write_record(storage, result_key, outputs=[asset_output])
    storage.select_current_record(
        result_key,
        candidate_record_id=record_id,
        attempt_id="attempt",
        run_id="run_001",
    )

    run_path = storage.write_run_metadata(
        "run_001",
        workflow_identity="workflow-a",
        engine="local",
        status="running",
        target_nodes=["Segment_1"],
        started_at="2026-06-16T10:00:00Z",
    )
    result_path = storage.write_run_node_result(
        "run_001",
        "Segment_1",
        result_key=result_key,
        record_id=record_id,
        cache_hit=False,
    )

    run = json.loads(run_path.read_text())
    assert run["schema"] == "bioimageflow.run.v1"
    assert run["run_id"] == "run_001"
    assert run["workflow_identity"] == "workflow-a"
    assert run["target_nodes"] == ["Segment_1"]

    result = json.loads(result_path.read_text())
    assert result["schema"] == "bioimageflow.run.node_result.v1"
    assert result["run_id"] == "run_001"
    assert result["node_key"] == "Segment_1"
    assert result["result_key"] == result_key
    assert result["record_id"] == record_id
    assert result["cache_hit"] is False
    assert result["canonical"].endswith(
        f"cache/v1/results/{result_key[3:5]}/{result_key[5:7]}/{result_key}/records/{record_id}"
    )
    assert result["outputs"] == [asset_output]

    node_dir = tmp_path / "views" / "runs" / "run_001" / "nodes" / "Segment_1"
    record_link = json.loads((node_dir / "record.bioimageflow-link.json").read_text())
    assert record_link == {
        "schema": "bioimageflow.link.v1",
        "kind": "directory",
        "target": result["canonical"],
    }
    output_link = json.loads(
        (
            node_dir / "outputs" / "assets" / "mask.tif.bioimageflow-link.json"
        ).read_text()
    )
    assert output_link["schema"] == "bioimageflow.link.v1"
    assert output_link["kind"] == "file"
    assert output_link["target"].endswith(f"records/{record_id}/assets/mask.tif")


def test_run_node_result_view_exposes_scalar_outputs_without_links(
    tmp_path: Path,
) -> None:
    storage = Storage(tmp_path)
    result_key = make_result_key({"node": "hotspot"})
    scalar_output = {
        "kind": "scalar_output",
        "output_column": "spot_count",
        "row_index": "0",
        "value": {"kind": "signed_integer", "value": "0"},
    }
    record_id = _write_record(storage, result_key, outputs=[scalar_output])
    storage.select_current_record(
        result_key,
        candidate_record_id=record_id,
        attempt_id="attempt",
        run_id="run_scalar",
    )
    storage.write_run_metadata(
        "run_scalar",
        workflow_identity="workflow-scalar",
        engine="local",
        status="running",
        target_nodes=["HotspotToSpots_1"],
    )

    result_path = storage.write_run_node_result(
        "run_scalar",
        "HotspotToSpots_1",
        result_key=result_key,
        record_id=record_id,
        cache_hit=False,
    )

    result = json.loads(result_path.read_text())
    assert result["outputs"] == [scalar_output]
    assert not (
        tmp_path
        / "views"
        / "runs"
        / "run_scalar"
        / "nodes"
        / "HotspotToSpots_1"
        / "outputs"
    ).exists()


def test_latest_views_point_to_run_views_and_successful_run(tmp_path: Path) -> None:
    storage = Storage(tmp_path)
    result_key = make_result_key({"node": "table"})
    record_id = _write_record(storage, result_key)
    storage.select_current_record(
        result_key,
        candidate_record_id=record_id,
        attempt_id="attempt",
        run_id="run_002",
    )
    storage.write_run_metadata(
        "run_002",
        workflow_identity="workflow-b",
        engine="local",
        status="succeeded",
        target_nodes=["Table_1"],
        started_at="2026-06-16T10:00:00Z",
        completed_at="2026-06-16T10:00:01Z",
    )
    storage.write_run_node_result(
        "run_002",
        "Table_1",
        result_key=result_key,
        record_id=record_id,
        cache_hit=True,
    )

    latest_node_path = storage.update_latest_node("Table_1", "run_002")
    latest_success_path = storage.update_latest_success_run("run_002")

    latest_node = json.loads(latest_node_path.read_text())
    assert latest_node == {
        "schema": "bioimageflow.link.v1",
        "kind": "directory",
        "target": "../runs/run_002/nodes/Table_1",
    }
    latest_success = json.loads(latest_success_path.read_text())
    assert latest_success == {
        "schema": "bioimageflow.link.v1",
        "kind": "directory",
        "target": "run_002",
    }
    assert (
        latest_node_path
        == tmp_path / "views" / "latest" / "Table_1.bioimageflow-link.json"
    )
    assert (
        latest_success_path
        == tmp_path / "views" / "runs" / "latest-success.bioimageflow-link.json"
    )
    assert not (tmp_path / "runs").exists()
    assert not (tmp_path / "latest").exists()
