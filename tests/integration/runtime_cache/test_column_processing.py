"""Focused tests split from ``tests/integration/test_runtime_cache.py``."""

# ruff: noqa: F401

import json

import hashlib

import logging

from pathlib import Path

from typing import Annotated

import pandas as pd

import pytest

from bioimageflow import NodePlanStatus, ProgressEvent, Workflow

from bioimageflow.dataframe_tool import DataFrameTool

from bioimageflow.cache import (
    dataframe_result_key,
    processing_prepare_attempt,
    processing_publish,
)

from bioimageflow.storage import (
    CacheCorruptionError,
    CurrentPointer,
    RecordManifest,
    Storage,
    make_record_id,
)

from bioimageflow_core import (
    Arguments,
    EnvironmentSpec,
    ExecutionContext,
    IOModel,
    ProcessingTool,
    Template,
)

from bioimageflow_core.types import ImageSpec, Semantic, SharedArray


from tests.testkit.runtime_cache import (
    ColumnBoundAssetWriter,
    ColumnBoundZeroRowScalarWriter,
    ColumnBoundZeroRowWriter,
    CountingTable,
    EscapingColumnBoundWriter,
    MultiRowTable,
    _invalidated_node_names,
    _planned_result_key,
    _run_dirs,
    _selection_for,
)


def test_column_bound_processing_tool_publishes_owned_asset_record_and_uses_cache_hit(
    tmp_path: Path,
) -> None:
    storage_path = tmp_path / "results"
    ColumnBoundAssetWriter.executions = 0
    events: list[tuple[str, str]] = []

    with Workflow(engine="direct", storage_path=storage_path) as wf:
        table = CountingTable()(value=4)
        node = ColumnBoundAssetWriter()(label=table["label"])
        first = wf.compute(node)
        result_key = _planned_result_key(wf, node.name)

    storage = Storage(storage_path)
    pointer = storage.load_current(result_key)
    assert pointer is not None
    record_dir = storage.result_dir(result_key) / "records" / pointer.record_id
    manifest = json.loads((record_dir / "manifest.json").read_text())
    assert manifest["outputs"] == [
        {
            "digest": manifest["outputs"][0]["digest"],
            "kind": "owned_asset",
            "path": "assets/label_row.txt",
            "size": 2,
        }
    ]
    assert Path(first.loc["row", "output"]) == record_dir / "assets" / "label_row.txt"
    assert Path(first.loc["row", "output"]).read_text() == "v4"

    with Workflow(
        engine="direct",
        storage_path=storage_path,
        on_progress=lambda event: events.append((event.node_name, event.status)),
    ) as wf:
        table = CountingTable()(value=4)
        second = wf.compute(ColumnBoundAssetWriter()(label=table["label"]))

    pd.testing.assert_frame_equal(first, second)
    assert ColumnBoundAssetWriter.executions == 1
    assert ("ColumnBoundAssetWriter_1", "cached") in events


def test_column_bound_zero_row_processing_tool_publishes_written_template_assets(
    tmp_path: Path,
) -> None:
    storage_path = tmp_path / "results"
    ColumnBoundZeroRowWriter.executions = 0

    with Workflow(engine="direct", storage_path=storage_path) as wf:
        table = MultiRowTable()()
        node = ColumnBoundZeroRowWriter()(label=table["label"])
        first = wf.compute(node)
        result_key = _planned_result_key(wf, node.name)

    assert first.empty
    assert list(first.columns) == ["output", "count"]
    storage = Storage(storage_path)
    pointer = storage.load_current(result_key)
    assert pointer is not None
    record_dir = storage.result_dir(result_key) / "records" / pointer.record_id
    manifest = json.loads((record_dir / "manifest.json").read_text())
    assert [output["path"] for output in manifest["outputs"]] == [
        "assets/zero_a.txt",
        "assets/zero_b.txt",
    ]
    assert [output["output_column"] for output in manifest["outputs"]] == [
        "output",
        "output",
    ]
    assert [output["row_index"] for output in manifest["outputs"]] == ["a", "b"]
    assert (record_dir / "assets" / "zero_a.txt").read_text() == "alpha"
    assert (record_dir / "assets" / "zero_b.txt").read_text() == "beta"

    with Workflow(engine="direct", storage_path=storage_path) as wf:
        table = MultiRowTable()()
        second = wf.compute(ColumnBoundZeroRowWriter()(label=table["label"]))

    pd.testing.assert_frame_equal(first, second)
    assert ColumnBoundZeroRowWriter.executions == 2


def test_column_bound_zero_row_processing_tool_publishes_declared_scalar_outputs(
    tmp_path: Path,
) -> None:
    storage_path = tmp_path / "results"
    ColumnBoundZeroRowScalarWriter.executions = 0

    with Workflow(engine="direct", storage_path=storage_path) as wf:
        table = MultiRowTable()()
        node = ColumnBoundZeroRowScalarWriter()(label=table["label"])
        result = wf.compute(node)
        result_key = _planned_result_key(wf, node.name)
        node_name = node.name

    assert result.empty
    assert list(result.columns) == ["spot_id", "spot_count"]
    assert ColumnBoundZeroRowScalarWriter.executions == 2
    storage = Storage(storage_path)
    pointer = storage.load_current(result_key)
    assert pointer is not None
    record_dir = storage.result_dir(result_key) / "records" / pointer.record_id
    manifest = json.loads((record_dir / "manifest.json").read_text())
    assert manifest["outputs"] == [
        {
            "kind": "scalar_output",
            "output_column": "spot_count",
            "row_index": "a",
            "value": {"kind": "signed_integer", "value": "0"},
        },
        {
            "kind": "scalar_output",
            "output_column": "spot_count",
            "row_index": "b",
            "value": {"kind": "signed_integer", "value": "0"},
        },
    ]

    [run_dir] = _run_dirs(storage_path)
    run_result = json.loads((run_dir / "nodes" / node_name / "result.json").read_text())
    assert run_result["outputs"] == manifest["outputs"]
    assert not (run_dir / "nodes" / node_name / "outputs").exists()

    with Workflow(engine="direct", storage_path=storage_path) as wf:
        table = MultiRowTable()()
        second = wf.compute(ColumnBoundZeroRowScalarWriter()(label=table["label"]))

    pd.testing.assert_frame_equal(result, second)
    assert ColumnBoundZeroRowScalarWriter.executions == 2


def test_column_bound_processing_tool_plan_reports_cached_from_current(
    tmp_path: Path,
) -> None:
    storage_path = tmp_path / "results"

    with Workflow(engine="direct", storage_path=storage_path) as wf:
        table = CountingTable()(value=5)
        wf.compute(ColumnBoundAssetWriter()(label=table["label"]))

    with Workflow(engine="direct", storage_path=storage_path) as wf:
        table = CountingTable()(value=5)
        node = ColumnBoundAssetWriter()(label=table["label"])
        plan = wf.plan()

    assert plan[node.name].status is NodePlanStatus.CACHED


def test_column_bound_processing_tool_invalidate_removes_current_but_keeps_record(
    tmp_path: Path,
) -> None:
    storage_path = tmp_path / "results"

    with Workflow(engine="direct", storage_path=storage_path) as wf:
        table = CountingTable()(value=6)
        node = ColumnBoundAssetWriter()(label=table["label"])
        wf.compute(node)
        node_name = node.name
        result_key = _planned_result_key(wf, node_name)

    storage = Storage(storage_path)
    pointer = storage.load_current(result_key)
    assert pointer is not None
    record_dir = storage.result_dir(result_key) / "records" / pointer.record_id

    with Workflow(engine="direct", storage_path=storage_path) as wf:
        table = CountingTable()(value=6)
        node = ColumnBoundAssetWriter()(label=table["label"])
        cleared = wf.invalidate([node.name])

    assert _invalidated_node_names(cleared) == {node_name}
    assert storage.load_current(result_key) is None
    assert record_dir.exists()


def test_column_bound_processing_tool_invalidate_removes_prior_current_selection(
    tmp_path: Path,
) -> None:
    storage_path = tmp_path / "results"

    with Workflow(engine="direct", storage_path=storage_path) as wf:
        table = CountingTable()(value=9)
        node = ColumnBoundAssetWriter()(
            label=table["label"], output_templates={"output": "first_{row_index}.txt"}
        )
        wf.compute(node)
        prior_result_key = _planned_result_key(wf, node.name)

    storage = Storage(storage_path)
    assert storage.load_current(prior_result_key) is not None

    with Workflow(engine="direct", storage_path=storage_path) as wf:
        table = CountingTable()(value=9)
        node = ColumnBoundAssetWriter()(
            label=table["label"], output_templates={"output": "second_{row_index}.txt"}
        )
        cleared = wf.invalidate([node.name])

    assert _invalidated_node_names(cleared) == {"ColumnBoundAssetWriter_1"}
    selection = _selection_for(cleared, "ColumnBoundAssetWriter_1")
    assert selection.result_key == prior_result_key
    assert selection.status == "removed"
    assert storage.load_current(prior_result_key) is None

    with Workflow(engine="direct", storage_path=storage_path) as wf:
        table = CountingTable()(value=9)
        node = ColumnBoundAssetWriter()(
            label=table["label"], output_templates={"output": "first_{row_index}.txt"}
        )
        plan = wf.plan()

    assert plan[node.name].status is NodePlanStatus.UNEXECUTED


def test_column_bound_processing_tool_invalidate_keeps_unrelated_metadata_less_current(
    tmp_path: Path,
) -> None:
    storage_path = tmp_path / "results"

    with Workflow(engine="direct", storage_path=storage_path) as wf:
        table = CountingTable()(value=10)
        node_a = ColumnBoundAssetWriter()(label=table["label"], name="writer_a")
        node_b = ColumnBoundAssetWriter()(label=table["label"], name="writer_b")
        wf.compute(node_a, node_b)
        plan = wf.plan()

    storage = Storage(storage_path)
    key_a = plan["writer_a"].final_result_key
    key_b = plan["writer_b"].final_result_key
    assert key_a is not None
    assert key_b is not None
    (storage.result_dir(key_a) / "result.json").unlink()
    (storage.result_dir(key_b) / "result.json").unlink()

    with Workflow(engine="direct", storage_path=storage_path) as wf:
        table = CountingTable()(value=10)
        node_a = ColumnBoundAssetWriter()(label=table["label"], name="writer_a")
        ColumnBoundAssetWriter()(label=table["label"], name="writer_b")
        cleared = wf.invalidate([node_a.name], cascade=False)

    assert _invalidated_node_names(cleared) == {"writer_a"}
    assert storage.load_current(key_a) is None
    assert storage.load_current(key_b) is not None


def test_column_bound_processing_tool_preserves_nested_owned_asset_paths(
    tmp_path: Path,
) -> None:
    storage_path = tmp_path / "results"

    with Workflow(engine="direct", storage_path=storage_path) as wf:
        table = MultiRowTable()()
        node = ColumnBoundAssetWriter()(
            label=table["label"],
            output_templates={"output": "nested/{column:label}/{row_index}.txt"},
        )
        df = wf.compute(node)
        result_key = _planned_result_key(wf, node.name)

    storage = Storage(storage_path)
    pointer = storage.load_current(result_key)
    assert pointer is not None
    record_dir = storage.result_dir(result_key) / "records" / pointer.record_id
    manifest = json.loads((record_dir / "manifest.json").read_text())
    assert [output["path"] for output in manifest["outputs"]] == [
        "assets/nested/alpha/a.txt",
        "assets/nested/beta/b.txt",
    ]
    assert set(df["output"]) == {
        str(record_dir / "assets" / "nested" / "alpha" / "a.txt"),
        str(record_dir / "assets" / "nested" / "beta" / "b.txt"),
    }


@pytest.mark.parametrize(
    "template", ["../outside.txt", "/absolute.txt", r"nested\\backslash.txt"]
)
def test_column_bound_processing_tool_rejects_unsafe_output_template_before_execution(
    tmp_path: Path,
    template: str,
) -> None:
    storage_path = tmp_path / "results"
    ColumnBoundAssetWriter.executions = 0

    with Workflow(engine="direct", storage_path=storage_path) as wf:
        table = CountingTable()(value=7)
        node = ColumnBoundAssetWriter()(
            label=table["label"], output_templates={"output": template}
        )
        with pytest.raises(CacheCorruptionError):
            wf.compute(node)
        result_key = _planned_result_key(wf, node.name)

    assert ColumnBoundAssetWriter.executions == 0
    assert not (Storage(storage_path).result_dir(result_key) / "current.json").exists()


def test_column_bound_processing_tool_rejects_templated_output_outside_staging(
    tmp_path: Path,
) -> None:
    storage_path = tmp_path / "results"

    with Workflow(engine="direct", storage_path=storage_path) as wf:
        table = CountingTable()(value=8)
        node = EscapingColumnBoundWriter()(label=table["label"], directory=tmp_path)
        with pytest.raises(CacheCorruptionError):
            wf.compute(node)
        result_key = _planned_result_key(wf, node.name)

    assert not (Storage(storage_path).result_dir(result_key) / "current.json").exists()
