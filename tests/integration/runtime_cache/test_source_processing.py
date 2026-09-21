"""Focused tests split from ``tests/integration/runtime_cache/test_source_processing.py``."""

import json


from pathlib import Path


import pandas as pd
import pytest


from bioimageflow import NodePlanStatus, ProgressEvent, Workflow


from bioimageflow.storage import (
    CacheCorruptionError,
    Storage,
)


from tests.testkit.runtime_cache import (
    DefaultTemplateZeroRowWriter,
    DynamicSourceAssets,
    SourceAssetWriter,
    SourceExternalPaths,
    ZeroRowAssetWriter,
    _current_pointer_files,
    _invalidated_node_names,
    _planned_result_key,
    _selection_for,
)


def test_source_processing_tool_publishes_owned_asset_record_and_uses_cache_hit(
    tmp_path: Path,
) -> None:
    storage_path = tmp_path / "results"
    SourceAssetWriter.executions = 0
    events: list[tuple[str, str]] = []

    with Workflow(engine="direct", storage_path=storage_path) as wf:
        node = SourceAssetWriter()(text="abc")
        first = wf.compute(node)
        result_key = _planned_result_key(wf, node.name)

    storage = Storage(storage_path)
    pointer = storage.load_current(result_key)
    assert pointer is not None
    record_dir = storage.result_dir(result_key) / "records" / pointer.record_id
    manifest = json.loads((record_dir / "manifest.json").read_text())
    assert manifest["outputs"] == [
        {
            "asset_type": "file",
            "digest": manifest["outputs"][0]["digest"],
            "kind": "owned_asset",
            "path": "assets/mask_0.txt",
            "size": 3,
        }
    ]
    assert (record_dir / "assets" / "mask_0.txt").read_text() == "abc"
    assert first.loc["0", "mask"] == str(record_dir / "assets" / "mask_0.txt")

    with Workflow(
        engine="direct",
        storage_path=storage_path,
        on_progress=lambda event: events.append((event.node_name, event.status)),
    ) as wf:
        second = wf.compute(SourceAssetWriter()(text="abc"))

    pd.testing.assert_frame_equal(first, second)
    assert SourceAssetWriter.executions == 1
    assert ("SourceAssetWriter_1", "cached") in events


def test_dynamic_context_assets_are_record_owned_and_cwd_independent(
    tmp_path: Path, monkeypatch
) -> None:
    storage_path = tmp_path / "results"
    DynamicSourceAssets.executions = 0

    with Workflow(engine="direct", storage_path=storage_path) as wf:
        node = DynamicSourceAssets()()
        first = wf.compute(node)
        result_key = _planned_result_key(wf, node.name)

    storage = Storage(storage_path)
    pointer = storage.load_current(result_key)
    assert pointer is not None
    record_dir = storage.result_dir(result_key) / "records" / pointer.record_id
    manifest = json.loads((record_dir / "manifest.json").read_text())
    path_schema = next(
        column
        for column in manifest["dataframe"]["logical_schema"]
        if column["name"] == "path"
    )
    assert path_schema["kind"] == "record_asset"
    assert {entry["path"] for entry in manifest["outputs"]} == {
        "assets/alpha.txt",
        "assets/beta.txt",
        "assets/gamma.txt",
    }

    other_cwd = tmp_path / "other-cwd"
    other_cwd.mkdir()
    monkeypatch.chdir(other_cwd)
    with Workflow(engine="direct", storage_path=storage_path) as wf:
        node = DynamicSourceAssets()()
        assert wf.plan()[node.name].status is NodePlanStatus.CACHED
        second = wf.compute(node)

    pd.testing.assert_frame_equal(first, second)
    assert DynamicSourceAssets.executions == 1


def test_zero_row_processing_tool_publishes_written_template_asset_without_sentinel_row(
    tmp_path: Path,
) -> None:
    storage_path = tmp_path / "results"
    ZeroRowAssetWriter.executions = 0

    with Workflow(engine="direct", storage_path=storage_path) as wf:
        node = ZeroRowAssetWriter()(text="blank")
        first = wf.compute(node)
        result_key = _planned_result_key(wf, node.name)

    assert first.empty
    assert list(first.columns) == ["mask", "count"]
    storage = Storage(storage_path)
    pointer = storage.load_current(result_key)
    assert pointer is not None
    record_dir = storage.result_dir(result_key) / "records" / pointer.record_id
    manifest = json.loads((record_dir / "manifest.json").read_text())
    assert manifest["outputs"] == [
        {
            "asset_type": "file",
            "digest": manifest["outputs"][0]["digest"],
            "kind": "owned_asset",
            "output_column": "mask",
            "path": "assets/mask_0.txt",
            "row_index": "0",
            "size": 5,
        }
    ]
    assert (record_dir / "assets" / "mask_0.txt").read_text() == "blank"

    with Workflow(engine="direct", storage_path=storage_path) as wf:
        second = wf.compute(ZeroRowAssetWriter()(text="blank"))

    pd.testing.assert_frame_equal(first, second)
    assert ZeroRowAssetWriter.executions == 1


def test_zero_row_processing_tool_publishes_written_default_template_asset(
    tmp_path: Path,
) -> None:
    storage_path = tmp_path / "results"
    DefaultTemplateZeroRowWriter.executions = 0

    with Workflow(engine="direct", storage_path=storage_path) as wf:
        node = DefaultTemplateZeroRowWriter()(text="default")
        result = wf.compute(node)
        result_key = _planned_result_key(wf, node.name)

    assert result.empty
    storage = Storage(storage_path)
    pointer = storage.load_current(result_key)
    assert pointer is not None
    record_dir = storage.result_dir(result_key) / "records" / pointer.record_id
    manifest = json.loads((record_dir / "manifest.json").read_text())
    assert manifest["outputs"] == [
        {
            "asset_type": "file",
            "digest": manifest["outputs"][0]["digest"],
            "kind": "owned_asset",
            "output_column": "output",
            "path": "assets/DefaultTemplateZeroRowWriter_1_0",
            "row_index": "0",
            "size": 7,
        }
    ]
    assert (
        record_dir / "assets" / "DefaultTemplateZeroRowWriter_1_0"
    ).read_text() == "default"
    assert DefaultTemplateZeroRowWriter.executions == 1


def test_processing_tool_progress_events_expose_selection_identity(
    tmp_path: Path,
) -> None:
    storage_path = tmp_path / "results"
    SourceAssetWriter.executions = 0
    events: list[ProgressEvent] = []

    with Workflow(
        engine="direct", storage_path=storage_path, on_progress=events.append
    ) as wf:
        node = SourceAssetWriter()(text="progress")
        wf.compute(node)
        node_name = node.name

    [current_path] = _current_pointer_files(storage_path)
    current = json.loads(current_path.read_text())
    started = [
        event
        for event in events
        if event.node_name == node_name and event.status == "started"
    ]
    completed = [
        event
        for event in events
        if event.node_name == node_name and event.status == "completed"
    ]
    assert started[-1].result_key == current["result_key"]
    assert started[-1].record_id is None
    assert completed[-1].result_key == current["result_key"]
    assert completed[-1].record_id == current["record_id"]

    events.clear()
    with Workflow(
        engine="direct", storage_path=storage_path, on_progress=events.append
    ) as wf:
        wf.compute(SourceAssetWriter()(text="progress"))

    cached = [
        event
        for event in events
        if event.node_name == node_name and event.status == "cached"
    ]
    assert cached[-1].result_key == current["result_key"]
    assert cached[-1].record_id == current["record_id"]
    assert SourceAssetWriter.executions == 1


def test_source_processing_tool_external_paths_stay_external_references(
    tmp_path: Path,
) -> None:
    source_dir = tmp_path / "input"
    source_dir.mkdir()
    (source_dir / "a.txt").write_text("a")
    (source_dir / "b.txt").write_text("b")
    storage_path = tmp_path / "results"
    SourceExternalPaths.executions = 0

    with Workflow(engine="direct", storage_path=storage_path) as wf:
        node = SourceExternalPaths()(directory=source_dir)
        first = wf.compute(node)
        result_key = _planned_result_key(wf, node.name)

    storage = Storage(storage_path)
    pointer = storage.load_current(result_key)
    assert pointer is not None
    record_dir = storage.result_dir(result_key) / "records" / pointer.record_id
    manifest = json.loads((record_dir / "manifest.json").read_text())
    assert manifest["outputs"] == [
        {
            "identity": "path",
            "kind": "external_path",
            "path": str(source_dir / "a.txt"),
        },
        {
            "identity": "path",
            "kind": "external_path",
            "path": str(source_dir / "b.txt"),
        },
    ]
    assert set(first["path"]) == {str(source_dir / "a.txt"), str(source_dir / "b.txt")}
    assert not (record_dir / "assets").exists()

    with Workflow(engine="direct", storage_path=storage_path) as wf:
        second = wf.compute(SourceExternalPaths()(directory=source_dir))

    pd.testing.assert_frame_equal(first, second)
    assert SourceExternalPaths.executions == 1


def test_source_processing_tool_plan_reports_cached_from_current(
    tmp_path: Path,
) -> None:
    storage_path = tmp_path / "results"

    with Workflow(engine="direct", storage_path=storage_path) as wf:
        wf.compute(SourceAssetWriter()(text="plan"))

    with Workflow(engine="direct", storage_path=storage_path) as wf:
        node = SourceAssetWriter()(text="plan")
        plan = wf.plan()

    assert plan[node.name].status is NodePlanStatus.CACHED


def test_source_processing_tool_default_input_value_affects_signature(
    tmp_path: Path,
) -> None:
    storage_path = tmp_path / "results"

    with Workflow(engine="direct", storage_path=storage_path) as wf:
        explicit = SourceAssetWriter()(text="mask", name="source")
        wf.compute(explicit)
        explicit_logical_signature = wf.plan()[explicit.name].logical_signature

    with Workflow(engine="direct", storage_path=storage_path) as wf:
        defaulted = SourceAssetWriter()(name="source")
        plan = wf.plan()

    assert plan[defaulted.name].logical_signature == explicit_logical_signature
    assert plan[defaulted.name].status is NodePlanStatus.CACHED


def test_source_processing_tool_empty_output_template_override_does_not_claim_external_path(
    tmp_path: Path,
) -> None:
    source_dir = tmp_path / "input"
    source_dir.mkdir()
    (source_dir / "a.txt").write_text("a")
    storage_path = tmp_path / "results"

    with Workflow(engine="direct", storage_path=storage_path) as wf:
        node = SourceExternalPaths()(
            directory=source_dir, output_templates={"path": ""}
        )
        df = wf.compute(node)
        result_key = _planned_result_key(wf, node.name)

    pointer = Storage(storage_path).load_current(result_key)
    assert pointer is not None
    assert list(df["path"]) == [str(source_dir / "a.txt")]


def test_source_processing_tool_invalidate_removes_current_but_keeps_record(
    tmp_path: Path,
) -> None:
    storage_path = tmp_path / "results"
    SourceAssetWriter.executions = 0

    with Workflow(engine="direct", storage_path=storage_path) as wf:
        node = SourceAssetWriter()(text="invalidate")
        wf.compute(node)
        node_name = node.name
        result_key = _planned_result_key(wf, node_name)

    storage = Storage(storage_path)
    pointer = storage.load_current(result_key)
    assert pointer is not None
    record_dir = storage.result_dir(result_key) / "records" / pointer.record_id

    with Workflow(engine="direct", storage_path=storage_path) as wf:
        node = SourceAssetWriter()(text="invalidate")
        cleared = wf.invalidate([node.name])

    assert _invalidated_node_names(cleared) == {node_name}
    selection = _selection_for(cleared, node_name)
    assert selection.result_key == result_key
    assert selection.selected_record_id == pointer.record_id
    assert selection.status == "removed"
    assert storage.load_current(result_key) is None
    assert record_dir.exists()

    with Workflow(engine="direct", storage_path=storage_path) as wf:
        wf.compute(SourceAssetWriter()(text="invalidate"))

    assert SourceAssetWriter.executions == 2


def test_source_processing_tool_invalidate_removes_corrupt_current(
    tmp_path: Path,
) -> None:
    storage_path = tmp_path / "results"

    with Workflow(engine="direct", storage_path=storage_path) as wf:
        node = SourceAssetWriter()(text="corrupt")
        wf.compute(node)
        node_name = node.name
        result_key = _planned_result_key(wf, node_name)

    current_path = Storage(storage_path).result_dir(result_key) / "current.json"
    current_path.write_text("{not json")

    with Workflow(engine="direct", storage_path=storage_path) as wf:
        node = SourceAssetWriter()(text="corrupt")
        cleared = wf.invalidate([node.name])

    assert _invalidated_node_names(cleared) == {node_name}
    selection = _selection_for(cleared, node_name)
    assert selection.result_key == result_key
    assert selection.selected_record_id is None
    assert selection.status == "corrupt_removed"
    assert not current_path.exists()


def test_source_processing_tool_invalidate_removes_corrupt_current_with_default_inputs(
    tmp_path: Path,
) -> None:
    storage_path = tmp_path / "results"

    with Workflow(engine="direct", storage_path=storage_path) as wf:
        node = SourceAssetWriter()()
        wf.compute(node)
        node_name = node.name
        result_key = _planned_result_key(wf, node_name)

    current_path = Storage(storage_path).result_dir(result_key) / "current.json"
    current_path.write_text("{not json")

    with Workflow(engine="direct", storage_path=storage_path) as wf:
        node = SourceAssetWriter()()
        cleared = wf.invalidate([node.name])

    assert _invalidated_node_names(cleared) == {node_name}
    assert not current_path.exists()


def test_corrupt_pointer_does_not_quarantine_its_valid_record(tmp_path: Path) -> None:
    storage_path = tmp_path / "results"

    with Workflow(engine="direct", storage_path=storage_path) as wf:
        node = SourceAssetWriter()(text="valid-record")
        wf.compute(node)
        result_key = _planned_result_key(wf, node.name)

    storage = Storage(storage_path)
    pointer = storage.load_current(result_key)
    assert pointer is not None
    record_dir = storage.result_dir(result_key) / "records" / pointer.record_id
    current_path = storage.result_dir(result_key) / "current.json"
    raw_pointer = json.loads(current_path.read_text())
    raw_pointer["manifest"] = "../unsafe-manifest.json"
    current_path.write_text(json.dumps(raw_pointer))

    with Workflow(engine="direct", storage_path=storage_path) as wf:
        node = SourceAssetWriter()(text="valid-record")
        selection = _selection_for(wf.invalidate([node.name]), node.name)

    assert selection.status == "corrupt_removed"
    assert selection.selected_record_id == pointer.record_id
    assert record_dir.is_dir()
    assert not (storage.result_dir(result_key) / "quarantine").exists()


def test_corrupt_selected_record_is_diagnostic_in_plan_and_quarantined_on_clear(
    tmp_path: Path,
) -> None:
    storage_path = tmp_path / "results"
    SourceAssetWriter.executions = 0

    with Workflow(engine="direct", storage_path=storage_path) as wf:
        node = SourceAssetWriter()(text="repair")
        wf.compute(node)
        node_name = node.name
        result_key = _planned_result_key(wf, node_name)

    storage = Storage(storage_path)
    pointer = storage.load_current(result_key)
    assert pointer is not None
    record_dir = storage.result_dir(result_key) / "records" / pointer.record_id
    (record_dir / "assets" / "mask_0.txt").write_text("corrupt")

    with Workflow(engine="direct", storage_path=storage_path) as wf:
        node = SourceAssetWriter()(text="repair")
        plan = wf.plan()
        assert plan[node.name].status is NodePlanStatus.CORRUPT
        assert plan[node.name].diagnostic is not None
        with pytest.raises(CacheCorruptionError):
            wf.compute(node)
        cleared = wf.invalidate([node.name])

    selection = _selection_for(cleared, node_name)
    assert selection.status == "corrupt_removed"
    assert not record_dir.exists()
    quarantined = list(
        (storage.result_dir(result_key) / "quarantine").glob(f"{pointer.record_id}.*")
    )
    assert len(quarantined) == 1

    with Workflow(engine="direct", storage_path=storage_path) as wf:
        node = SourceAssetWriter()(text="repair")
        repaired = wf.compute(node)
        assert wf.plan()[node.name].status is NodePlanStatus.CACHED

    assert Path(repaired.loc["0", "mask"]).read_text() == "repair"
    assert SourceAssetWriter.executions == 2
