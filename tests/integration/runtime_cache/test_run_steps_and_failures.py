"""Focused tests split from ``tests/integration/runtime_cache/test_run_views.py``."""

import json


from pathlib import Path


import pytest

from bioimageflow import ProgressEvent, Workflow


from tests.testkit.runtime_cache import (
    CountingTable,
    FailingDataFrameTool,
    FailingSourceAssetWriter,
    SlowSourceAssetWriter,
    SourceAssetWriter,
    _latest_success_run_dir,
    _run_dirs,
)


def test_compute_steps_writes_run_view_for_cached_step(tmp_path: Path) -> None:
    storage_path = tmp_path / "results"
    CountingTable.executions = 0

    with Workflow(engine="direct", storage_path=storage_path) as wf:
        node = CountingTable()(value=13)
        wf.compute(node)
        node_name = node.name
    assert CountingTable.executions == 1

    events: list[ProgressEvent] = []
    with Workflow(
        engine="direct", storage_path=storage_path, on_progress=events.append
    ) as wf:
        node = CountingTable()(value=13)
        steps = wf.compute_steps(node)
        step = next(steps)
        assert step.cached is True
        step.execute()
        with pytest.raises(StopIteration):
            next(steps)

    assert CountingTable.executions == 1
    latest_run = _latest_success_run_dir(storage_path)
    result = json.loads((latest_run / "nodes" / node_name / "result.json").read_text())
    assert result["cache_hit"] is True
    cached = [
        event
        for event in events
        if event.node_name == node_name and event.status == "cached"
    ]
    assert cached[-1].result_key == result["result_key"]
    assert cached[-1].record_id == result["record_id"]


def test_compute_steps_processing_tool_cached_step_emits_progress_identity(
    tmp_path: Path,
) -> None:
    storage_path = tmp_path / "results"
    SourceAssetWriter.executions = 0

    with Workflow(engine="direct", storage_path=storage_path) as wf:
        node = SourceAssetWriter()(text="step")
        wf.compute(node)
        node_name = node.name
    assert SourceAssetWriter.executions == 1

    events: list[ProgressEvent] = []
    with Workflow(
        engine="direct", storage_path=storage_path, on_progress=events.append
    ) as wf:
        node = SourceAssetWriter()(text="step")
        steps = wf.compute_steps(node)
        step = next(steps)
        assert step.cached is True
        step.execute()
        with pytest.raises(StopIteration):
            next(steps)

    assert SourceAssetWriter.executions == 1
    latest_run = _latest_success_run_dir(storage_path)
    result = json.loads((latest_run / "nodes" / node_name / "result.json").read_text())
    assert result["cache_hit"] is True
    cached = [
        event
        for event in events
        if event.node_name == node_name and event.status == "cached"
    ]
    assert cached[-1].result_key == result["result_key"]
    assert cached[-1].record_id == result["record_id"]


def test_compute_steps_auto_execute_writes_successful_run_view(tmp_path: Path) -> None:
    storage_path = tmp_path / "results"

    with Workflow(engine="direct", storage_path=storage_path) as wf:
        node = CountingTable()(value=14)
        steps = wf.compute_steps(node)
        step = next(steps)
        assert step.node_name == node.name
        with pytest.raises(StopIteration):
            next(steps)
        node_name = node.name

    latest_run = _latest_success_run_dir(storage_path)
    result = json.loads((latest_run / "nodes" / node_name / "result.json").read_text())
    assert result["cache_hit"] is False


def test_failed_compute_keeps_successful_node_latest_without_latest_success(
    tmp_path: Path,
) -> None:
    storage_path = tmp_path / "results"

    with Workflow(engine="direct", storage_path=storage_path) as wf:
        table = CountingTable()(value=15)
        failing = FailingDataFrameTool()(table)
        with pytest.raises(RuntimeError, match="planned failure"):
            wf.compute(failing)
        table_name = table.name
        failing_name = failing.name

    [run_dir] = _run_dirs(storage_path)
    run = json.loads((run_dir / "run.json").read_text())
    assert run["status"] == "failed"
    assert (run_dir / "nodes" / table_name / "result.json").exists()
    assert not (run_dir / "nodes" / failing_name / "result.json").exists()
    assert not (
        storage_path / "views" / "runs" / "latest-success.bioimageflow-link.json"
    ).exists()
    latest_node = json.loads(
        (
            storage_path / "views" / "latest" / f"{table_name}.bioimageflow-link.json"
        ).read_text()
    )
    assert latest_node["target"] == f"../runs/{run_dir.name}/nodes/{table_name}"


def test_failed_parallel_compute_keeps_successful_sibling_run_view(
    tmp_path: Path,
) -> None:
    storage_path = tmp_path / "results"

    with Workflow(engine="direct", storage_path=storage_path) as wf:
        slow_success = SlowSourceAssetWriter()(text="ok")
        fast_failure = FailingSourceAssetWriter()(text="boom")
        with pytest.raises(RuntimeError, match="boom"):
            wf.compute(slow_success, fast_failure)
        success_name = slow_success.name

    [run_dir] = _run_dirs(storage_path)
    run = json.loads((run_dir / "run.json").read_text())
    assert run["status"] == "failed"
    assert (run_dir / "nodes" / success_name / "result.json").exists()
    assert not (
        storage_path / "views" / "runs" / "latest-success.bioimageflow-link.json"
    ).exists()
    latest_node = json.loads(
        (
            storage_path / "views" / "latest" / f"{success_name}.bioimageflow-link.json"
        ).read_text()
    )
    assert latest_node["target"] == f"../runs/{run_dir.name}/nodes/{success_name}"
