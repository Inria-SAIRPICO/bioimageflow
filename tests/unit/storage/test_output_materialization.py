"""Focused tests split from ``tests/unit/storage/test_output_views.py``."""

from __future__ import annotations


import json


from pathlib import Path


import pytest

from bioimageflow.storage import (
    CacheCorruptionError,
    Storage,
    make_result_key,
)

from tests.testkit.storage import (
    _file_digest,
    _write_record,
)


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

    output_path = tmp_path / "outputs" / "latest" / "Segment_1" / "mask.tif"
    assert materialized == [output_path]
    assert output_path.read_bytes() == b"mask"
    assert not output_path.is_symlink()


def test_latest_output_mapping_preserves_nested_legacy_and_scoped_paths(
    tmp_path: Path,
) -> None:
    storage = Storage(tmp_path)
    result_key = make_result_key({"node": "nested"})
    outputs = [
        {
            "path": path,
            "kind": "owned_asset",
            "size": 4,
            "digest": _file_digest(b"mask"),
            "asset_type": "file",
        }
        for path in ["assets/masks/nuclei/t051.tiff", "legacy/results/table.csv"]
    ]
    record_id = _write_record(storage, result_key, outputs=outputs)
    storage.select_current_record(
        result_key,
        candidate_record_id=record_id,
        attempt_id="attempt",
        run_id="run_nested",
    )
    storage.write_run_metadata(
        "run_nested",
        workflow_identity="workflow-nested",
        engine="local",
        status="succeeded",
        target_nodes=["parent/segment"],
    )
    storage.write_run_node_result(
        "run_nested",
        "parent/segment",
        result_key=result_key,
        record_id=record_id,
        cache_hit=False,
    )
    storage.update_latest_node("parent/segment", "run_nested")

    materialized = storage.materialize_latest_outputs("copy")

    latest_node = tmp_path / "outputs" / "latest" / "parent" / "segment"
    assert materialized == [
        latest_node / "masks" / "nuclei" / "t051.tiff",
        latest_node / "legacy" / "results" / "table.csv",
    ]
    assert all(path.read_bytes() == b"mask" for path in materialized)


def test_latest_output_mapping_rejects_collisions_before_replacement(
    tmp_path: Path,
) -> None:
    storage = Storage(tmp_path)
    result_key = make_result_key({"node": "collision"})
    outputs = [
        {
            "path": path,
            "kind": "owned_asset",
            "size": 4,
            "digest": _file_digest(b"mask"),
            "asset_type": "file",
        }
        for path in ["assets/file.tiff", "file.tiff"]
    ]
    record_id = _write_record(storage, result_key, outputs=outputs)
    storage.select_current_record(
        result_key,
        candidate_record_id=record_id,
        attempt_id="attempt",
        run_id="run_collision",
    )
    storage.write_run_metadata(
        "run_collision",
        workflow_identity="workflow-collision",
        engine="local",
        status="succeeded",
        target_nodes=["Segment_1"],
    )
    storage.write_run_node_result(
        "run_collision",
        "Segment_1",
        result_key=result_key,
        record_id=record_id,
        cache_hit=False,
    )
    storage.update_latest_node("Segment_1", "run_collision")
    previous = tmp_path / "outputs" / "latest" / "Segment_1" / "previous.txt"
    previous.parent.mkdir(parents=True)
    previous.write_text("previous")

    with pytest.raises(CacheCorruptionError, match="collide"):
        storage.materialize_latest_outputs("copy")

    assert previous.read_text() == "previous"


def test_pointer_mode_uses_simplified_latest_and_unchanged_run_paths(
    tmp_path: Path,
) -> None:
    storage = Storage(tmp_path)
    result_key = make_result_key({"node": "pointer"})
    output = {
        "path": "assets/masks/t051.tiff",
        "kind": "owned_asset",
        "size": 4,
        "digest": _file_digest(b"mask"),
        "asset_type": "file",
    }
    record_id = _write_record(storage, result_key, outputs=[output])
    storage.select_current_record(
        result_key,
        candidate_record_id=record_id,
        attempt_id="attempt",
        run_id="run_pointer",
    )
    storage.write_run_metadata(
        "run_pointer",
        workflow_identity="workflow-pointer",
        engine="local",
        status="succeeded",
        target_nodes=["Segment_1"],
    )
    storage.write_run_node_result(
        "run_pointer",
        "Segment_1",
        result_key=result_key,
        record_id=record_id,
        cache_hit=False,
    )
    storage.update_latest_node("Segment_1", "run_pointer")

    latest = storage.materialize_latest_outputs("pointer")
    runs = storage.materialize_run_outputs("run_pointer", "pointer")

    latest_path = (
        tmp_path
        / "outputs"
        / "latest"
        / "Segment_1"
        / "masks"
        / "t051.tiff.bioimageflow-link.json"
    )
    run_path = (
        tmp_path
        / "outputs"
        / "runs"
        / "run_pointer"
        / "nodes"
        / "Segment_1"
        / "outputs"
        / "assets"
        / "masks"
        / "t051.tiff.bioimageflow-link.json"
    )
    assert latest == [latest_path]
    assert runs == [run_path]
    for pointer_path in [latest_path, run_path]:
        pointer = json.loads(pointer_path.read_text())
        assert pointer["schema"] == "bioimageflow.link.v1"
        assert pointer["kind"] == "file"
        assert not Path(pointer["target"]).is_absolute()
        resolved = (pointer_path.parent / pointer["target"]).resolve()
        assert resolved.read_bytes() == b"mask"
        resolved.relative_to(tmp_path.resolve())


@pytest.mark.parametrize("mode", ["symlink", "copy", "hardlink"])
def test_latest_link_and_copy_modes_use_simplified_path(
    tmp_path: Path, mode: str
) -> None:
    storage = Storage(tmp_path)
    capability = storage.probe_output_view_mode(mode)
    if not capability.supported:
        pytest.skip(f"{mode} is unavailable: {capability.code}")
    result_key = make_result_key({"node": mode})
    output = {
        "path": "assets/nested/mask.tif",
        "kind": "owned_asset",
        "size": 4,
        "digest": _file_digest(b"mask"),
        "asset_type": "file",
    }
    record_id = _write_record(storage, result_key, outputs=[output])
    storage.select_current_record(
        result_key,
        candidate_record_id=record_id,
        attempt_id="attempt",
        run_id=f"run_{mode}",
    )
    storage.write_run_metadata(
        f"run_{mode}",
        workflow_identity=f"workflow-{mode}",
        engine="local",
        status="succeeded",
        target_nodes=["Segment_1"],
    )
    storage.write_run_node_result(
        f"run_{mode}",
        "Segment_1",
        result_key=result_key,
        record_id=record_id,
        cache_hit=False,
    )
    storage.update_latest_node("Segment_1", f"run_{mode}")

    [output_path] = storage.materialize_latest_outputs(mode)

    assert (
        output_path
        == tmp_path / "outputs" / "latest" / "Segment_1" / "nested" / "mask.tif"
    )
    assert output_path.read_bytes() == b"mask"
    assert output_path.is_symlink() is (mode == "symlink")
    if mode == "hardlink":
        source = (
            storage.result_dir(result_key)
            / "records"
            / record_id
            / "assets"
            / "nested"
            / "mask.tif"
        )
        assert output_path.stat().st_ino == source.stat().st_ino


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

    output_path = (
        tmp_path
        / "outputs"
        / "runs"
        / "run_symlink"
        / "nodes"
        / "Segment_1"
        / "outputs"
        / "assets"
        / "mask.tif"
    )
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

    output_path = (
        tmp_path
        / "outputs"
        / "runs"
        / "run_hardlink"
        / "nodes"
        / "Segment_1"
        / "outputs"
        / "assets"
        / "mask.tif"
    )
    source_path = (
        storage.result_dir(result_key) / "records" / record_id / "assets" / "mask.tif"
    )
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
