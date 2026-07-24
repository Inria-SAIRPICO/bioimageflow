"""Focused tests split from ``tests/integration/runtime_cache/test_run_views.py``."""

import json

import logging

from pathlib import Path

import pandas as pd

import pytest

from bioimageflow import ProgressEvent, Workflow, export_outputs

from bioimageflow.storage import (
    Storage,
)

from tests.testkit.runtime_cache import (
    CountingTable,
    DoubleValue,
    FailingDataFrameTool,
    SourceAssetWriter,
    _current_pointer_files,
    _latest_success_run_dir,
    _run_dirs,
)


@pytest.mark.compat
def test_dataframe_tool_compute_publishes_record_and_uses_cache_hit(
    tmp_path: Path,
) -> None:
    storage_path = tmp_path / "results"
    CountingTable.executions = 0

    with Workflow(engine="direct", storage_path=storage_path) as wf:
        table = CountingTable()(value=7)
        first = wf.compute(table)

    current_files = _current_pointer_files(storage_path)
    assert len(current_files) == 1
    current = json.loads(current_files[0].read_text())
    result_key = current["result_key"]
    record_id = current["record_id"]
    storage = Storage(storage_path)
    pointer = storage.load_current(result_key)
    assert pointer is not None
    assert pointer.record_id == record_id
    assert (
        storage.result_dir(result_key) / "records" / record_id / "dataframe.parquet"
    ).exists()

    events: list[tuple[str, str]] = []
    with Workflow(
        engine="direct",
        storage_path=storage_path,
        on_progress=lambda event: events.append((event.node_name, event.status)),
    ) as wf:
        table = CountingTable()(value=7)
        second = wf.compute(table)

    pd.testing.assert_frame_equal(first, second)
    assert CountingTable.executions == 1
    assert ("CountingTable_1", "cached") in events
    assert _current_pointer_files(storage_path) == current_files


def test_dataframe_tool_progress_events_expose_selection_identity(
    tmp_path: Path,
) -> None:
    storage_path = tmp_path / "results"
    CountingTable.executions = 0
    events: list[ProgressEvent] = []

    with Workflow(
        engine="direct", storage_path=storage_path, on_progress=events.append
    ) as wf:
        node = CountingTable()(value=19)
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
        wf.compute(CountingTable()(value=19))

    cached = [
        event
        for event in events
        if event.node_name == node_name and event.status == "cached"
    ]
    assert cached[-1].result_key == current["result_key"]
    assert cached[-1].record_id == current["record_id"]
    assert CountingTable.executions == 1


def test_compute_writes_run_view_for_dataframe_cache_miss_and_hit(
    tmp_path: Path,
) -> None:
    storage_path = tmp_path / "results"
    CountingTable.executions = 0

    with Workflow(engine="direct", storage_path=storage_path) as wf:
        node = CountingTable()(value=12)
        wf.compute(node)
        node_name = node.name

    [first_run] = _run_dirs(storage_path)
    first_run_metadata = json.loads((first_run / "run.json").read_text())
    assert first_run_metadata["schema"] == "bioimageflow.run.v1"
    assert first_run_metadata["status"] == "succeeded"
    assert first_run_metadata["target_nodes"] == [node_name]
    first_result = json.loads(
        (first_run / "nodes" / node_name / "result.json").read_text()
    )
    assert first_result["schema"] == "bioimageflow.run.node_result.v1"
    assert first_result["node_key"] == node_name
    assert first_result["cache_hit"] is False
    assert (
        Storage(storage_path).load_current(first_result["result_key"]).record_id
        == first_result["record_id"]
    )
    assert (first_run / "nodes" / node_name / "record.bioimageflow-link.json").exists()
    assert _latest_success_run_dir(storage_path) == first_run

    with Workflow(engine="direct", storage_path=storage_path) as wf:
        node = CountingTable()(value=12)
        wf.compute(node)

    assert CountingTable.executions == 1
    second_run = [path for path in _run_dirs(storage_path) if path != first_run][0]
    second_result = json.loads(
        (second_run / "nodes" / node_name / "result.json").read_text()
    )
    assert second_result["result_key"] == first_result["result_key"]
    assert second_result["record_id"] == first_result["record_id"]
    assert second_result["cache_hit"] is True
    latest_node = json.loads(
        (
            storage_path / "views" / "latest" / f"{node_name}.bioimageflow-link.json"
        ).read_text()
    )
    assert latest_node["target"] == f"../runs/{second_run.name}/nodes/{node_name}"
    assert _latest_success_run_dir(storage_path) == second_run


def test_compute_writes_run_view_output_pointers_for_processing_assets(
    tmp_path: Path,
) -> None:
    storage_path = tmp_path / "results"

    with Workflow(engine="direct", storage_path=storage_path) as wf:
        node = SourceAssetWriter()(text="view")
        wf.compute(node)
        node_name = node.name

    [run_dir] = _run_dirs(storage_path)
    result = json.loads((run_dir / "nodes" / node_name / "result.json").read_text())
    assert result["cache_hit"] is False
    assert result["outputs"][0]["kind"] == "owned_asset"
    asset_path = result["outputs"][0]["path"]
    output_link_path = (
        run_dir
        / "nodes"
        / node_name
        / "outputs"
        / f"{asset_path}.bioimageflow-link.json"
    )
    output_link = json.loads(output_link_path.read_text())
    assert output_link["schema"] == "bioimageflow.link.v1"
    assert output_link["kind"] == "file"
    assert output_link["digest"] == result["outputs"][0]["digest"]
    assert output_link["target"].endswith(f"records/{result['record_id']}/{asset_path}")


def test_compute_with_output_view_materializes_latest_outputs(tmp_path: Path) -> None:
    storage_path = tmp_path / "results"

    with Workflow(engine="direct", storage_path=storage_path, output_view="copy") as wf:
        node = SourceAssetWriter()(text="visible")
        wf.compute(node)
        node_name = node.name

    [run_dir] = _run_dirs(storage_path)
    result = json.loads((run_dir / "nodes" / node_name / "result.json").read_text())
    asset_path = result["outputs"][0]["path"]
    latest_output = (
        storage_path
        / "outputs"
        / "latest"
        / node_name
        / Path(asset_path).relative_to("assets")
    )

    assert latest_output.read_text() == "visible"
    assert not latest_output.is_symlink()
    provenance = json.loads((latest_output.parent / "provenance.json").read_text())
    assert provenance["run"]["status"] == "succeeded"
    assert provenance["run"]["completed_at"] is not None
    assert (
        storage_path / "views" / "latest" / f"{node_name}.bioimageflow-link.json"
    ).exists()


def test_compute_with_output_view_materializes_run_outputs(tmp_path: Path) -> None:
    storage_path = tmp_path / "results"

    with Workflow(
        engine="direct",
        storage_path=storage_path,
        output_view={"mode": "copy", "scope": "runs"},
    ) as wf:
        node = SourceAssetWriter()(text="run-visible")
        wf.compute(node)
        node_name = node.name

    [run_dir] = _run_dirs(storage_path)
    result = json.loads((run_dir / "nodes" / node_name / "result.json").read_text())
    asset_path = result["outputs"][0]["path"]
    run_output = (
        storage_path
        / "outputs"
        / "runs"
        / run_dir.name
        / "nodes"
        / node_name
        / "outputs"
        / asset_path
    )

    assert run_output.read_text() == "run-visible"
    assert not (storage_path / "outputs" / "latest").exists()


def test_export_outputs_materializes_after_compute(tmp_path: Path) -> None:
    storage_path = tmp_path / "results"

    with Workflow(engine="direct", storage_path=storage_path) as wf:
        node = SourceAssetWriter()(text="manual")
        wf.compute(node)
        node_name = node.name
        materialized = wf.export_outputs(mode="copy", scope="latest")

    output_path = storage_path / "outputs" / "latest" / node_name / "mask_0.txt"
    assert materialized == [
        output_path,
        output_path.parent / "dataframe.parquet",
        output_path.parent / "dataframe.csv",
        output_path.parent / "dataframe.json",
        output_path.parent / "provenance.json",
    ]
    assert output_path.read_text() == "manual"
    exported = json.loads((output_path.parent / "dataframe.json").read_text())
    assert exported["columns"] == ["mask", "count"]
    assert exported["data"][0][0] == "mask_0.txt"
    provenance = json.loads((output_path.parent / "provenance.json").read_text())
    assert provenance["computation"]["tool"]["class"] == "SourceAssetWriter"


def test_exported_provenance_identifies_selected_upstream_record(
    tmp_path: Path,
) -> None:
    storage_path = tmp_path / "results"
    with Workflow(engine="direct", storage_path=storage_path) as wf:
        source = CountingTable()(value=9)
        derived = DoubleValue()(source)
        wf.compute(derived)
        wf.export_outputs(mode="copy", scope="latest")

    provenance = json.loads(
        (
            storage_path
            / "outputs"
            / "latest"
            / derived.name
            / "provenance.json"
        ).read_text()
    )
    [provider] = provenance["computation"]["inputs"]["argument_0"]["providers"]
    assert provider["provider"]["node_key"] == source.name
    assert provider["provider"]["result_key"].startswith("rk_")
    assert provider["provider"]["record_id"].startswith("rec_")


def test_export_run_outputs_uses_latest_success_pointer(tmp_path: Path) -> None:
    storage_path = tmp_path / "results"

    with Workflow(engine="direct", storage_path=storage_path) as wf:
        node = SourceAssetWriter()(text="latest-success")
        wf.compute(node)
        node_name = node.name

    materialized = export_outputs(storage_path, mode="copy", scope="runs")

    [run_dir] = _run_dirs(storage_path)
    output_path = (
        storage_path
        / "outputs"
        / "runs"
        / run_dir.name
        / "nodes"
        / node_name
        / "outputs"
        / "assets"
        / "mask_0.txt"
    )
    assert materialized == [
        output_path,
        output_path.parents[1] / "dataframe.parquet",
        output_path.parents[1] / "dataframe.csv",
        output_path.parents[1] / "dataframe.json",
        output_path.parents[1] / "provenance.json",
    ]
    assert output_path.read_text() == "latest-success"


def test_latest_output_view_ignores_stale_latest_pointers_after_invalidation(
    tmp_path: Path,
) -> None:
    storage_path = tmp_path / "results"

    with Workflow(engine="direct", storage_path=storage_path, output_view="copy") as wf:
        stale = SourceAssetWriter()(text="stale")
        wf.compute(stale)
        wf.invalidate([stale.name])
        fresh = SourceAssetWriter()(text="fresh")
        wf.compute(fresh)
        fresh_name = fresh.name

    fresh_output = storage_path / "outputs" / "latest" / fresh_name / "mask_0.txt"
    assert fresh_output.read_text() == "fresh"


def test_failed_run_scope_output_view_preserves_original_error(tmp_path: Path) -> None:
    storage_path = tmp_path / "results"

    with Workflow(
        engine="direct",
        storage_path=storage_path,
        output_view={"mode": "hardlink", "scope": "runs"},
    ) as wf:
        table = SourceAssetWriter()(text="ok")
        failing = FailingDataFrameTool()(table)
        with pytest.raises(RuntimeError, match="planned failure"):
            wf.compute(failing)

    assert not (storage_path / "outputs" / "runs").exists()


def test_automatic_output_view_failure_warns_without_failing_compute(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    def fail_latest(self, node_key: str, mode: str) -> list[Path]:
        raise OSError("simulated automatic export failure")

    monkeypatch.setattr(Storage, "materialize_latest_node_outputs", fail_latest)
    storage_path = tmp_path / "results"

    with caplog.at_level(logging.WARNING, logger="bioimageflow"):
        with Workflow(
            engine="direct", storage_path=storage_path, output_view="copy"
        ) as wf:
            node = SourceAssetWriter()(text="computed")
            result = wf.compute(node)

    assert Path(result.iloc[0]["mask"]).read_text() == "computed"
    assert "Automatic output-view materialization failed" in caplog.text


def test_manual_output_export_failure_remains_strict(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage_path = tmp_path / "results"
    with Workflow(engine="direct", storage_path=storage_path) as wf:
        node = SourceAssetWriter()(text="computed")
        wf.compute(node)

        def fail_latest(self, mode: str) -> list[Path]:
            raise OSError("simulated explicit export failure")

        monkeypatch.setattr(Storage, "materialize_latest_outputs", fail_latest)
        with pytest.raises(OSError, match="simulated explicit export failure"):
            wf.export_outputs(mode="copy", scope="latest")
