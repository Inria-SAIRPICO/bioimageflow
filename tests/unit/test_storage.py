"""Contract tests for output/cache storage primitives."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pandas as pd
import pytest

from bioimageflow.storage import (
    CacheCorruptionError,
    CurrentPointer,
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


def _record_id_for(result_key: str, dataframe_digest: str, outputs: list[dict[str, object]]) -> str:
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
    (record_dir / "manifest.json").write_text(json.dumps(manifest.to_dict(), indent=2, sort_keys=True))
    return record_id


def test_make_node_keys_normalizes_and_disambiguates() -> None:
    keys = make_node_keys(["Segment", "segment", "sub/work", " café ", "cafe\u0301"])

    assert keys["Segment"].startswith("segment")
    assert keys["Segment"] != keys["segment"]
    assert "/" not in keys["sub/work"]
    assert keys[" café "] != keys["cafe\u0301"]
    assert all("/" not in key for key in keys.values())


@pytest.mark.parametrize(
    "segment",
    ["", ".", "..", "/abs", "a\\b", "CON", "NUL", "name\x00"],
)
def test_validate_relative_posix_path_rejects_unsafe_segments(segment: str) -> None:
    with pytest.raises(ValueError):
        validate_relative_posix_path(segment)


def test_validate_relative_posix_path_allows_nested_safe_paths() -> None:
    assert validate_relative_posix_path("assets/mask.tif") == "assets/mask.tif"


def test_result_key_and_shard_are_deterministic() -> None:
    material = {"tool": "Threshold", "params": {"value": 10}, "upstream": ["rec_1"]}

    key = make_result_key(material)

    assert key == make_result_key({"upstream": ["rec_1"], "params": {"value": 10}, "tool": "Threshold"})
    assert key.startswith("rk_")
    shard = result_shard_parts(key)
    assert shard == (key[3:5], key[5:7])


@pytest.mark.parametrize("result_key", ["rk_ab/cdef", "rk_abc..def", "xx_abcdef", "rk_ab"])
def test_result_shard_rejects_unsafe_result_keys(result_key: str) -> None:
    with pytest.raises(ValueError):
        result_shard_parts(result_key)


def test_canonical_json_bytes_are_stable() -> None:
    assert canonical_json_bytes({"b": 1, "a": [2, 3]}) == b'{"a":[2,3],"b":1}'


def test_canonical_dataframe_digest_is_stable_and_rejects_objects() -> None:
    df = pd.DataFrame(
        {
            "text": ["Cafe\u0301", "same"],
            "number": [1, 2],
            "float": [float("nan"), float("inf")],
        },
        index=["row1", "row2"],
    )

    digest = canonical_dataframe_digest(df)

    assert digest.startswith("sha256:")
    assert digest == canonical_dataframe_digest(df[["float", "number", "text"]])

    with pytest.raises(TypeError, match="Unsupported dataframe value"):
        canonical_dataframe_digest(pd.DataFrame({"bad": [object()]}))


def test_canonical_dataframe_digest_includes_schema_material() -> None:
    df = pd.DataFrame(
        {
            "b": pd.Series([1], dtype="uint64"),
            "a": [pd.Timestamp("2026-06-16T12:00:00+02:00")],
            "asset": ["assets/mask.tif"],
        }
    )

    digest = canonical_dataframe_digest(
        df,
        declared_columns=["b", "a"],
        column_kinds={"asset": "record_asset"},
    )

    assert digest == canonical_dataframe_digest(
        df[["asset", "a", "b"]],
        declared_columns=["b", "a"],
        column_kinds={"asset": "record_asset"},
    )
    assert digest != canonical_dataframe_digest(df)
    assert canonical_dataframe_digest(pd.DataFrame({"n": pd.Series([1], dtype="uint64")})) != canonical_dataframe_digest(
        pd.DataFrame({"n": pd.Series([1], dtype="int64")})
    )


def test_canonical_dataframe_digest_rejects_unsafe_record_asset_path() -> None:
    with pytest.raises(ValueError):
        canonical_dataframe_digest(
            pd.DataFrame({"asset": ["../escape.tif"]}),
            column_kinds={"asset": "record_asset"},
        )


def test_canonical_scalar_payload_matches_dataframe_cell_encoding() -> None:
    assert canonical_scalar_payload(0) == {"kind": "signed_integer", "value": "0"}
    assert canonical_scalar_payload("Cafe\u0301") == {"kind": "string", "value": "Caf\u00e9"}
    assert canonical_scalar_payload(None) == {"kind": "null", "value": None}


def test_record_manifest_validation_checks_files_and_digest(tmp_path: Path) -> None:
    result_key = make_result_key({"node": "segment"})
    dataframe_digest = _file_digest(b"parquet")
    output = {"path": "assets/mask.tif", "kind": "owned_asset", "size": 4, "digest": _file_digest(b"not-mask")}
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

    valid_output = {"path": "assets/mask.tif", "kind": "owned_asset", "size": 4, "digest": _file_digest(b"mask")}
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

    bad_record_id = _record_id_for(result_key, dataframe_digest, [{"path": "../escape.tif", "kind": "owned_asset", "size": 1}])
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
        {"kind": "scalar_output", "output_column": "", "row_index": "0", "value": {"kind": "signed_integer", "value": "0"}},
        {"kind": "scalar_output", "output_column": "spot_count", "row_index": "", "value": {"kind": "signed_integer", "value": "0"}},
        {"kind": "scalar_output", "output_column": "spot_count", "row_index": "0", "value": 0},
        {"kind": "scalar_output", "output_column": "spot_count", "row_index": "0", "value": {"kind": "external_path", "value": "/tmp/a"}},
        {"kind": "scalar_output", "output_column": "spot_count", "row_index": "0", "value": {"kind": "signed_integer", "value": "1.5"}},
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


def test_record_manifest_validation_checks_declared_dataframe_file_digest(tmp_path: Path) -> None:
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


def test_current_pointer_guarded_update_first_valid_policy(tmp_path: Path) -> None:
    storage = Storage(tmp_path)
    result_key = make_result_key({"node": "segment"})
    result_dir = storage.result_dir(result_key)
    record_a = _write_record(storage, result_key)

    selected = storage.select_current_record(
        result_key,
        candidate_record_id=record_a,
        attempt_id="attempt_a",
        run_id="run_a",
    )

    assert selected.record_id == record_a
    current = json.loads((result_dir / "current.json").read_text())
    assert current["policy"] == "first-valid"

    record_b = _write_record(storage, result_key, dataframe_content=b"other-parquet")
    selected_again = storage.select_current_record(
        result_key,
        candidate_record_id=record_b,
        attempt_id="attempt_b",
        run_id="run_b",
    )

    assert selected_again.record_id == record_a
    conflicts = list((result_dir / "conflicts").glob("*.json"))
    assert len(conflicts) == 1


def test_select_current_record_rejects_unsafe_record_id(tmp_path: Path) -> None:
    storage = Storage(tmp_path)
    result_key = make_result_key({"node": "segment"})

    with pytest.raises(ValueError):
        storage.select_current_record(
            result_key,
            candidate_record_id="../rec_escape",
            attempt_id="attempt",
            run_id="run",
        )


def test_select_current_record_rejects_invalid_manifest(tmp_path: Path) -> None:
    storage = Storage(tmp_path)
    result_key = make_result_key({"node": "segment"})
    record_id = make_record_id(
        {
            "schema": "bioimageflow.cache.record.v1",
            "result_key": result_key,
            "dataframe": {"path": "dataframe.parquet", "digest": "sha256:" + "1" * 64},
            "outputs": [],
        }
    )
    record_dir = storage.result_dir(result_key) / "records" / record_id
    record_dir.mkdir(parents=True)
    (record_dir / "manifest.json").write_text("{}")

    with pytest.raises(CacheCorruptionError):
        storage.select_current_record(
            result_key,
            candidate_record_id=record_id,
            attempt_id="attempt",
            run_id="run",
        )


def test_select_current_record_rejects_symlinked_record_directory(tmp_path: Path) -> None:
    storage = Storage(tmp_path)
    result_key = make_result_key({"node": "segment"})
    record_id = _record_id_for(result_key, _file_digest(b"parquet"), [])
    outside_record = tmp_path / "outside-record"
    outside_record.mkdir()
    (outside_record / "dataframe.parquet").write_bytes(b"parquet")
    manifest = RecordManifest(
        result_key=result_key,
        record_id=record_id,
        dataframe_digest=_file_digest(b"parquet"),
        outputs=[],
    )
    (outside_record / "manifest.json").write_text(json.dumps(manifest.to_dict(), indent=2, sort_keys=True))
    records_dir = storage.result_dir(result_key) / "records"
    records_dir.mkdir(parents=True)
    (records_dir / record_id).symlink_to(outside_record)

    with pytest.raises(CacheCorruptionError, match="escapes"):
        storage.select_current_record(
            result_key,
            candidate_record_id=record_id,
            attempt_id="attempt",
            run_id="run",
        )


@pytest.mark.parametrize(
    "pointer_payload",
    [
        pytest.param("{not json", id="corrupt-json"),
        pytest.param("[]", id="non-mapping"),
    ],
)
def test_load_current_raises_on_invalid_pointer_payload(tmp_path: Path, pointer_payload: str) -> None:
    storage = Storage(tmp_path)
    result_key = make_result_key({"node": "segment"})
    result_dir = storage.result_dir(result_key)
    result_dir.mkdir(parents=True)
    (result_dir / "current.json").write_text(pointer_payload)

    with pytest.raises(CacheCorruptionError):
        storage.load_current(result_key)


def test_current_pointer_from_dict_rejects_invalid_selected_by() -> None:
    result_key = make_result_key({"node": "segment"})
    record_id = _record_id_for(result_key, "sha256:" + "1" * 64, [])
    pointer = CurrentPointer(
        result_key=result_key,
        record_id=record_id,
        manifest=f"records/{record_id}/manifest.json",
        attempt_id="attempt",
        run_id="run",
    ).to_dict()
    pointer["selected_by"] = "bad"

    with pytest.raises(CacheCorruptionError):
        CurrentPointer.from_dict(pointer)


def test_load_current_raises_on_invalid_manifest_path(tmp_path: Path) -> None:
    storage = Storage(tmp_path)
    result_key = make_result_key({"node": "segment"})
    result_dir = storage.result_dir(result_key)
    result_dir.mkdir(parents=True)
    record_id = _record_id_for(result_key, "sha256:" + "1" * 64, [])
    pointer = CurrentPointer(
        result_key=result_key,
        record_id=record_id,
        manifest=f"records/{record_id}/manifest.json",
        attempt_id="attempt",
        run_id="run",
    ).to_dict()
    pointer["manifest"] = "../escape/manifest.json"
    (result_dir / "current.json").write_text(json.dumps(pointer))

    with pytest.raises(CacheCorruptionError):
        storage.load_current(result_key)


def test_load_current_validates_selected_manifest(tmp_path: Path) -> None:
    storage = Storage(tmp_path)
    result_key = make_result_key({"node": "segment"})
    record_id = _write_record(storage, result_key)
    selected = storage.select_current_record(
        result_key,
        candidate_record_id=record_id,
        attempt_id="attempt",
        run_id="run",
    )
    (storage.result_dir(result_key) / "records" / record_id / "dataframe.parquet").unlink()

    with pytest.raises(CacheCorruptionError):
        storage.load_current(selected.result_key)


def test_current_pointer_round_trip() -> None:
    result_key = make_result_key({"node": "segment"})
    record_id = _record_id_for(result_key, "sha256:" + "1" * 64, [])
    pointer = CurrentPointer(
        result_key=result_key,
        record_id=record_id,
        manifest=f"records/{record_id}/manifest.json",
        attempt_id="attempt",
        run_id="run",
    )

    assert CurrentPointer.from_dict(pointer.to_dict()).record_id == record_id


def test_run_metadata_and_node_result_view_write_selected_record(tmp_path: Path) -> None:
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
    assert result["canonical"].endswith(f"cache/v1/results/{result_key[3:5]}/{result_key[5:7]}/{result_key}/records/{record_id}")
    assert result["outputs"] == [asset_output]

    node_dir = tmp_path / "views" / "runs" / "run_001" / "nodes" / "Segment_1"
    record_link = json.loads((node_dir / "record.bioimageflow-link.json").read_text())
    assert record_link == {
        "schema": "bioimageflow.link.v1",
        "kind": "directory",
        "target": result["canonical"],
    }
    output_link = json.loads((node_dir / "outputs" / "assets" / "mask.tif.bioimageflow-link.json").read_text())
    assert output_link["schema"] == "bioimageflow.link.v1"
    assert output_link["kind"] == "file"
    assert output_link["target"].endswith(f"records/{record_id}/assets/mask.tif")


def test_run_node_result_view_exposes_scalar_outputs_without_links(tmp_path: Path) -> None:
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
    assert not (tmp_path / "views" / "runs" / "run_scalar" / "nodes" / "HotspotToSpots_1" / "outputs").exists()


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
    assert latest_node_path == tmp_path / "views" / "latest" / "Table_1.bioimageflow-link.json"
    assert latest_success_path == tmp_path / "views" / "runs" / "latest-success.bioimageflow-link.json"
    assert not (tmp_path / "runs").exists()
    assert not (tmp_path / "latest").exists()


def test_materialize_latest_outputs_copies_owned_assets(tmp_path: Path) -> None:
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
        run_id="run_copy",
    )
    storage.write_run_metadata(
        "run_copy",
        workflow_identity="workflow-copy",
        engine="local",
        status="succeeded",
        target_nodes=["Segment_1"],
    )
    storage.write_run_node_result(
        "run_copy",
        "Segment_1",
        result_key=result_key,
        record_id=record_id,
        cache_hit=False,
    )
    storage.update_latest_node("Segment_1", "run_copy")

    materialized = storage.materialize_latest_outputs("copy")

    output_path = tmp_path / "outputs" / "latest" / "Segment_1" / "outputs" / "assets" / "mask.tif"
    assert materialized == [output_path]
    assert output_path.read_bytes() == b"mask"
    assert not output_path.is_symlink()


def test_materialize_run_outputs_symlinks_owned_assets(tmp_path: Path) -> None:
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
        run_id="run_symlink",
    )
    storage.write_run_metadata(
        "run_symlink",
        workflow_identity="workflow-symlink",
        engine="local",
        status="succeeded",
        target_nodes=["Segment_1"],
    )
    storage.write_run_node_result(
        "run_symlink",
        "Segment_1",
        result_key=result_key,
        record_id=record_id,
        cache_hit=False,
    )

    try:
        materialized = storage.materialize_run_outputs("run_symlink", "symlink")
    except OSError as exc:
        pytest.skip(f"symlinks are not supported on this filesystem: {exc}")

    output_path = tmp_path / "outputs" / "runs" / "run_symlink" / "nodes" / "Segment_1" / "outputs" / "assets" / "mask.tif"
    assert materialized == [output_path]
    assert output_path.is_symlink()
    assert output_path.read_bytes() == b"mask"


def test_materialize_run_outputs_hardlinks_owned_files(tmp_path: Path) -> None:
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
        run_id="run_hardlink",
    )
    storage.write_run_metadata(
        "run_hardlink",
        workflow_identity="workflow-hardlink",
        engine="local",
        status="succeeded",
        target_nodes=["Segment_1"],
    )
    storage.write_run_node_result(
        "run_hardlink",
        "Segment_1",
        result_key=result_key,
        record_id=record_id,
        cache_hit=False,
    )

    try:
        materialized = storage.materialize_run_outputs("run_hardlink", "hardlink")
    except OSError as exc:
        pytest.skip(f"hardlinks are not supported on this filesystem: {exc}")

    output_path = tmp_path / "outputs" / "runs" / "run_hardlink" / "nodes" / "Segment_1" / "outputs" / "assets" / "mask.tif"
    source_path = storage.result_dir(result_key) / "records" / record_id / "assets" / "mask.tif"
    assert materialized == [output_path]
    assert output_path.read_bytes() == b"mask"
    assert output_path.stat().st_ino == source_path.stat().st_ino


def test_materialize_latest_rejects_pointer_escaping_storage(tmp_path: Path) -> None:
    storage = Storage(tmp_path)
    latest_path = tmp_path / "views" / "latest" / "Segment_1.bioimageflow-link.json"
    latest_path.parent.mkdir(parents=True)
    latest_path.write_text(
        json.dumps(
            {
                "schema": "bioimageflow.link.v1",
                "kind": "directory",
                "target": "../../../outside",
            }
        )
    )

    with pytest.raises(CacheCorruptionError, match="escapes storage root"):
        storage.materialize_latest_outputs("copy")


def test_materialize_run_outputs_validates_requested_run_id(tmp_path: Path) -> None:
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
        run_id="run_requested",
    )
    storage.write_run_metadata(
        "run_requested",
        workflow_identity="workflow-requested",
        engine="local",
        status="succeeded",
        target_nodes=["Segment_1"],
    )
    storage.write_run_node_result(
        "run_requested",
        "Segment_1",
        result_key=result_key,
        record_id=record_id,
        cache_hit=False,
    )
    result_path = storage.run_node_dir("run_requested", "Segment_1") / "result.json"
    result = json.loads(result_path.read_text())
    result["run_id"] = "run_other"
    result_path.write_text(json.dumps(result))

    with pytest.raises(CacheCorruptionError, match="run ID"):
        storage.materialize_run_outputs("run_requested", "copy")


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
    assert (tmp_path / "views" / "runs" / "run_004" / "nodes" / "outer" / "Segment_1" / "result.json").exists()
    assert latest_path == tmp_path / "views" / "latest" / "outer" / "Segment_1.bioimageflow-link.json"
    latest = json.loads(latest_path.read_text())
    assert latest["target"] == "../../runs/run_004/nodes/outer/Segment_1"


@pytest.mark.parametrize("status", ["running", "failed", "cancelled"])
def test_latest_success_requires_succeeded_run_metadata(tmp_path: Path, status: str) -> None:
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


def test_latest_success_rejects_malformed_or_mismatched_run_metadata(tmp_path: Path) -> None:
    storage = Storage(tmp_path)
    run_dir = storage.run_dir("run_006")
    run_dir.mkdir(parents=True)
    (run_dir / "run.json").write_text("{not json")

    with pytest.raises(CacheCorruptionError):
        storage.update_latest_success_run("run_006")

    (run_dir / "run.json").write_text(json.dumps({"schema": "bioimageflow.run.v1", "run_id": "other", "status": "succeeded"}))

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
    result["outputs"] = [{"kind": "scalar_output", "output_column": "count", "row_index": "0", "value": {"kind": "signed_integer", "value": "0"}}]
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
    link_path = storage.run_node_dir("run_008", "Segment_1") / "outputs" / "assets" / "mask.tif.bioimageflow-link.json"
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
            method(*args, workflow_identity="workflow", engine="local", status="running", target_nodes=[])
        elif method_name == "write_run_node_result":
            method(*args, result_key=make_result_key({"node": "segment"}), record_id="rec_" + "a" * 52, cache_hit=False)
        else:
            method(*args)
