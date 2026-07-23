"""Focused tests split from ``tests/unit/storage/test_output_views.py``."""

from __future__ import annotations


import json

import os

from pathlib import Path


import pytest

from bioimageflow.storage import (
    CacheCorruptionError,
    OutputViewCapability,
    Storage,
    make_result_key,
)

from tests.testkit.storage import (
    _file_digest,
    _write_record,
)


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


def test_latest_replacement_failure_preserves_previous_view(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = Storage(tmp_path)

    def publish_latest(result_key: str, run_id: str, path: str) -> None:
        output = {
            "path": path,
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
            run_id=run_id,
        )
        storage.write_run_metadata(
            run_id,
            workflow_identity=f"workflow-{run_id}",
            engine="local",
            status="succeeded",
            target_nodes=["Segment_1"],
        )
        storage.write_run_node_result(
            run_id,
            "Segment_1",
            result_key=result_key,
            record_id=record_id,
            cache_hit=False,
        )
        storage.update_latest_node("Segment_1", run_id)

    publish_latest(make_result_key({"version": "old"}), "run_old", "assets/old.tif")
    storage.materialize_latest_outputs("copy")
    old_output = tmp_path / "outputs" / "latest" / "Segment_1" / "old.tif"
    assert old_output.read_bytes() == b"mask"

    publish_latest(make_result_key({"version": "new"}), "run_new", "assets/new.tif")

    def fail_materialization(*args, **kwargs) -> None:
        raise OSError("simulated materialization failure")

    monkeypatch.setattr(storage, "_materialize_path", fail_materialization)
    with pytest.raises(OSError, match="simulated materialization failure"):
        storage.materialize_latest_outputs("copy")

    assert old_output.read_bytes() == b"mask"
    assert not (old_output.parent / "new.tif").exists()
    assert not list(old_output.parent.parent.glob(".Segment_1.*.tmp"))

    monkeypatch.undo()
    real_replace = os.replace
    latest_node = old_output.parent

    def fail_install(source, destination) -> None:
        source_path = Path(source)
        destination_path = Path(destination)
        if destination_path == latest_node and source_path.name.endswith(".tmp"):
            raise OSError("simulated replacement failure")
        real_replace(source, destination)

    monkeypatch.setattr(os, "replace", fail_install)
    with pytest.raises(OSError, match="simulated replacement failure"):
        storage.materialize_latest_outputs("copy")

    assert old_output.read_bytes() == b"mask"
    assert not (old_output.parent / "new.tif").exists()
    assert not list(old_output.parent.parent.glob(".Segment_1.*"))


def test_successful_latest_replacement_removes_stale_files(tmp_path: Path) -> None:
    storage = Storage(tmp_path)

    def publish_latest(version: str, path: str) -> None:
        result_key = make_result_key({"version": version})
        output = {
            "path": path,
            "kind": "owned_asset",
            "size": 4,
            "digest": _file_digest(b"mask"),
            "asset_type": "file",
        }
        record_id = _write_record(storage, result_key, outputs=[output])
        run_id = f"run_{version}"
        storage.select_current_record(
            result_key,
            candidate_record_id=record_id,
            attempt_id="attempt",
            run_id=run_id,
        )
        storage.write_run_metadata(
            run_id,
            workflow_identity=f"workflow-{version}",
            engine="local",
            status="succeeded",
            target_nodes=["Segment_1"],
        )
        storage.write_run_node_result(
            run_id,
            "Segment_1",
            result_key=result_key,
            record_id=record_id,
            cache_hit=False,
        )
        storage.update_latest_node("Segment_1", run_id)

    publish_latest("old", "assets/stale.tif")
    storage.materialize_latest_outputs("copy")
    publish_latest("new", "assets/current.tif")
    storage.materialize_latest_outputs("copy")

    latest = tmp_path / "outputs" / "latest" / "Segment_1"
    assert not (latest / "stale.tif").exists()
    assert (latest / "current.tif").read_bytes() == b"mask"


@pytest.mark.parametrize("mode", ["pointer", "copy", "hardlink"])
def test_probe_output_view_mode_uses_storage_and_cleans_artifacts(
    tmp_path: Path, mode: str
) -> None:
    storage_path = tmp_path / "storage"
    capability = Storage(storage_path).probe_output_view_mode(mode)

    assert capability == OutputViewCapability(mode=mode, supported=True, code="ok")
    assert not list(storage_path.glob(".output-view-probe-*"))


def test_probe_symlink_mode_checks_file_and_directory_links(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[bool] = []
    real_symlink = os.symlink

    def recording_symlink(src, dst, target_is_directory=False):
        calls.append(target_is_directory)
        return real_symlink(src, dst, target_is_directory=target_is_directory)

    monkeypatch.setattr(os, "symlink", recording_symlink)
    storage_path = tmp_path / "storage"
    capability = Storage(storage_path).probe_output_view_mode("symlink")

    if not capability.supported:
        pytest.skip(f"symlinks are unavailable: {capability.code}")
    assert calls == [False, True]
    assert not list(storage_path.glob(".output-view-probe-*"))


def test_probe_symlink_reports_windows_permission_denial(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def deny_symlink(*args, **kwargs) -> None:
        error = OSError("privilege not held")
        error.winerror = 1314  # type: ignore[attr-defined]
        raise error

    monkeypatch.setattr(os, "symlink", deny_symlink)
    storage_path = tmp_path / "storage"

    capability = Storage(storage_path).probe_output_view_mode("symlink")

    assert capability == OutputViewCapability(
        mode="symlink",
        supported=False,
        code="permission_denied",
        detail="Could not create and read file symlink.",
    )
    assert not list(storage_path.glob(".output-view-probe-*"))


def test_probe_rejects_invalid_mode_without_creating_storage(tmp_path: Path) -> None:
    storage_path = tmp_path / "storage"

    capability = Storage(storage_path).probe_output_view_mode("automatic")

    assert capability.code == "invalid_mode"
    assert capability.supported is False
    assert not storage_path.exists()
