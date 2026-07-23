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


def test_run_node_result_requires_selected_current_record(tmp_path: Path) -> None:
    storage = Storage(tmp_path)
    result_key = make_result_key({"node": "segment"})
    record_a = _write_record(storage, result_key)

    with pytest.raises(CacheCorruptionError, match="selected current"):
        storage.write_run_node_result(
            "run_003",
            "Segment_1",
            result_key=result_key,
            record_id=record_a,
            cache_hit=False,
        )

    storage.select_current_record(
        result_key,
        candidate_record_id=record_a,
        attempt_id="attempt_a",
        run_id="run_003",
    )
    record_b = _write_record(storage, result_key, dataframe_content=b"other-parquet")

    with pytest.raises(CacheCorruptionError, match="selected current"):
        storage.write_run_node_result(
            "run_003",
            "Segment_1",
            result_key=result_key,
            record_id=record_b,
            cache_hit=False,
        )


def test_run_view_writes_do_not_mutate_current_pointer(tmp_path: Path) -> None:
    storage = Storage(tmp_path)
    result_key = make_result_key({"node": "segment"})
    record_id = _write_record(storage, result_key)
    storage.select_current_record(
        result_key,
        candidate_record_id=record_id,
        attempt_id="attempt",
        run_id="run_004",
    )
    current_path = storage.result_dir(result_key) / "current.json"
    before = current_path.read_text()

    storage.write_run_metadata(
        "run_004",
        workflow_identity="workflow-d",
        engine="local",
        status="succeeded",
        target_nodes=["outer/Segment_1"],
    )
    storage.write_run_node_result(
        "run_004",
        "outer/Segment_1",
        result_key=result_key,
        record_id=record_id,
        cache_hit=True,
    )
    latest_path = storage.update_latest_node("outer/Segment_1", "run_004")

    assert current_path.read_text() == before
    assert (
        tmp_path
        / "views"
        / "runs"
        / "run_004"
        / "nodes"
        / "outer"
        / "Segment_1"
        / "result.json"
    ).exists()
    assert (
        latest_path
        == tmp_path / "views" / "latest" / "outer" / "Segment_1.bioimageflow-link.json"
    )
    latest = json.loads(latest_path.read_text())
    assert latest["target"] == "../../runs/run_004/nodes/outer/Segment_1"


@pytest.mark.parametrize("status", ["running", "failed", "cancelled"])
def test_latest_success_requires_succeeded_run_metadata(
    tmp_path: Path, status: str
) -> None:
    storage = Storage(tmp_path)
    storage.write_run_metadata(
        "run_005",
        workflow_identity="workflow-e",
        engine="local",
        status=status,
        target_nodes=[],
    )

    with pytest.raises(CacheCorruptionError, match="successful"):
        storage.update_latest_success_run("run_005")


def test_latest_success_rejects_malformed_or_mismatched_run_metadata(
    tmp_path: Path,
) -> None:
    storage = Storage(tmp_path)
    run_dir = storage.run_dir("run_006")
    run_dir.mkdir(parents=True)
    (run_dir / "run.json").write_text("{not json")

    with pytest.raises(CacheCorruptionError):
        storage.update_latest_success_run("run_006")

    (run_dir / "run.json").write_text(
        json.dumps(
            {"schema": "bioimageflow.run.v1", "run_id": "other", "status": "succeeded"}
        )
    )

    with pytest.raises(CacheCorruptionError, match="run ID"):
        storage.update_latest_success_run("run_006")


def test_latest_success_run_id_requires_succeeded_run(tmp_path: Path) -> None:
    storage = Storage(tmp_path)
    storage.write_run_metadata(
        "run_failed",
        workflow_identity="workflow-failed",
        engine="local",
        status="failed",
        target_nodes=[],
    )
    latest_path = tmp_path / "views" / "runs" / "latest-success.bioimageflow-link.json"
    latest_path.write_text(
        json.dumps(
            {
                "schema": "bioimageflow.link.v1",
                "kind": "directory",
                "target": "run_failed",
            }
        )
    )

    with pytest.raises(CacheCorruptionError, match="successful"):
        storage.latest_success_run_id()


def test_latest_node_validates_complete_run_node_view(tmp_path: Path) -> None:
    storage = Storage(tmp_path)
    result_key = make_result_key({"node": "segment"})
    record_id = _write_record(storage, result_key)
    storage.select_current_record(
        result_key,
        candidate_record_id=record_id,
        attempt_id="attempt",
        run_id="run_007",
    )
    storage.write_run_metadata(
        "run_007",
        workflow_identity="workflow-g",
        engine="local",
        status="failed",
        target_nodes=["Segment_1"],
    )
    storage.write_run_node_result(
        "run_007",
        "Segment_1",
        result_key=result_key,
        record_id=record_id,
        cache_hit=True,
    )
    node_dir = storage.run_node_dir("run_007", "Segment_1")
    (node_dir / "record.bioimageflow-link.json").unlink()

    with pytest.raises(CacheCorruptionError, match="record pointer"):
        storage.update_latest_node("Segment_1", "run_007")

    storage.write_run_node_result(
        "run_007",
        "Segment_1",
        result_key=result_key,
        record_id=record_id,
        cache_hit=True,
    )
    result = json.loads((node_dir / "result.json").read_text())
    result["schema"] = "wrong"
    (node_dir / "result.json").write_text(json.dumps(result))

    with pytest.raises(CacheCorruptionError, match="schema"):
        storage.update_latest_node("Segment_1", "run_007")

    storage.write_run_node_result(
        "run_007",
        "Segment_1",
        result_key=result_key,
        record_id=record_id,
        cache_hit=True,
    )
    result = json.loads((node_dir / "result.json").read_text())
    result["result_key"] = "not-a-result-key"
    (node_dir / "result.json").write_text(json.dumps(result))

    with pytest.raises(CacheCorruptionError, match="identifier"):
        storage.update_latest_node("Segment_1", "run_007")

    storage.write_run_node_result(
        "run_007",
        "Segment_1",
        result_key=result_key,
        record_id=record_id,
        cache_hit=True,
    )
    result = json.loads((node_dir / "result.json").read_text())
    result["outputs"] = [
        {
            "kind": "scalar_output",
            "output_column": "count",
            "row_index": "0",
            "value": {"kind": "signed_integer", "value": "0"},
        }
    ]
    (node_dir / "result.json").write_text(json.dumps(result))

    with pytest.raises(CacheCorruptionError, match="outputs"):
        storage.update_latest_node("Segment_1", "run_007")


def test_latest_node_validates_output_pointer_digest(tmp_path: Path) -> None:
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
        run_id="run_008",
    )
    storage.write_run_metadata(
        "run_008",
        workflow_identity="workflow-h",
        engine="local",
        status="failed",
        target_nodes=["Segment_1"],
    )
    storage.write_run_node_result(
        "run_008",
        "Segment_1",
        result_key=result_key,
        record_id=record_id,
        cache_hit=True,
    )
    link_path = (
        storage.run_node_dir("run_008", "Segment_1")
        / "outputs"
        / "assets"
        / "mask.tif.bioimageflow-link.json"
    )
    link = json.loads(link_path.read_text())
    link["digest"] = "sha256:" + "0" * 64
    link_path.write_text(json.dumps(link))

    with pytest.raises(CacheCorruptionError, match="digest"):
        storage.update_latest_node("Segment_1", "run_008")


@pytest.mark.parametrize(
    ("method_name", "args"),
    [
        ("write_run_metadata", ("../run",)),
        ("write_run_node_result", ("run", "../node")),
        ("update_latest_node", ("../node", "run")),
        ("update_latest_success_run", ("../run",)),
    ],
)
def test_run_view_primitives_reject_unsafe_path_segments(
    tmp_path: Path,
    method_name: str,
    args: tuple[str, ...],
) -> None:
    storage = Storage(tmp_path)
    method = getattr(storage, method_name)

    with pytest.raises(ValueError):
        if method_name == "write_run_metadata":
            method(
                *args,
                workflow_identity="workflow",
                engine="local",
                status="running",
                target_nodes=[],
            )
        elif method_name == "write_run_node_result":
            method(
                *args,
                result_key=make_result_key({"node": "segment"}),
                record_id="rec_" + "a" * 52,
                cache_hit=False,
            )
        else:
            method(*args)
