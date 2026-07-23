"""Focused tests split from ``tests/integration/runtime_cache/test_shared_memory.py``."""

import json


from pathlib import Path


import pandas as pd

import pytest

from bioimageflow import NodePlanStatus, Workflow


from bioimageflow.storage import (
    CacheCorruptionError,
    Storage,
)


from bioimageflow_core.types import SharedArray

from tests.testkit.runtime_cache import (
    ColumnBoundSharedMemoryWriter,
    CountingTable,
    MultiRowTable,
    SourceSharedMemoryConsumer,
    _invalidated_node_names,
    _planned_result_key,
)


@pytest.mark.shared_memory
def test_column_bound_shared_array_processing_tool_publishes_durable_asset_and_rehydrates_hits(
    tmp_path: Path,
) -> None:
    from bioimageflow_core.shm import open_shared_array

    storage_path = tmp_path / "results"
    ColumnBoundSharedMemoryWriter.executions = 0

    with Workflow(engine="direct", storage_path=storage_path) as wf:
        table = CountingTable()(value=4)
        node = ColumnBoundSharedMemoryWriter()(label=table["label"])
        first = wf.compute(node)
        result_key = _planned_result_key(wf, node.name)

    assert ColumnBoundSharedMemoryWriter.executions == 1
    assert isinstance(first.loc["row", "result"], SharedArray)
    first_ref = first.loc["row", "result"]
    with open_shared_array(first_ref) as array:
        assert array.shape == (2, 2)
        assert str(array.dtype) == "uint8"
        assert int(array.sum()) == 8

    storage = Storage(storage_path)
    pointer = storage.load_current(result_key)
    assert pointer is not None
    record_dir = storage.result_dir(result_key) / "records" / pointer.record_id
    manifest = json.loads((record_dir / "manifest.json").read_text())
    assert len(manifest["outputs"]) == 1
    [output] = manifest["outputs"]
    assert output == {
        "array": {
            "column": "result",
            "dtype": "uint8",
            "format": "npy",
            "order": "C",
            "row_index": "row",
            "shape": [2, 2],
        },
        "asset_role": "shared_array",
        "asset_type": "file",
        "digest": output["digest"],
        "kind": "owned_asset",
        "path": output["path"],
        "size": output["size"],
    }
    assert output["path"].startswith("assets/shm/result_")
    assert output["path"].endswith(".npy")
    assert (record_dir / output["path"]).exists()

    events: list[tuple[str, str]] = []
    with Workflow(
        engine="direct",
        storage_path=storage_path,
        on_progress=lambda event: events.append((event.node_name, event.status)),
    ) as wf:
        table = CountingTable()(value=4)
        second = wf.compute(ColumnBoundSharedMemoryWriter()(label=table["label"]))

    assert ColumnBoundSharedMemoryWriter.executions == 1
    assert isinstance(second.loc["row", "result"], SharedArray)
    second_ref = second.loc["row", "result"]
    assert second_ref.name != first_ref.name
    with open_shared_array(second_ref) as array:
        assert int(array.sum()) == 8
    assert ("ColumnBoundSharedMemoryWriter_1", "cached") in events

    with Workflow(engine="direct", storage_path=storage_path) as wf:
        table = CountingTable()(value=4)
        writer = ColumnBoundSharedMemoryWriter()(label=table["label"])
        consumer = SourceSharedMemoryConsumer()(result=writer["result"])
        steps = list(wf.compute_steps(consumer))
        writer_step = next(step for step in steps if step.node_name == writer.name)
        assert writer_step.cached is True
        cached_writer_df = writer_step.execute()
        assert isinstance(cached_writer_df.loc["row", "result"], SharedArray)
        consumer_step = next(step for step in steps if step.node_name == consumer.name)
        consumer_df = consumer_step.execute()

    assert consumer_df.loc["row", "total"] == 8
    assert ColumnBoundSharedMemoryWriter.executions == 1

    with Workflow(engine="direct", storage_path=storage_path) as wf:
        table = CountingTable()(value=4)
        node = ColumnBoundSharedMemoryWriter()(label=table["label"])
        assert wf.plan()[node.name].status is NodePlanStatus.CACHED
        cleared = wf.invalidate([node.name])

    assert _invalidated_node_names(cleared) == {"ColumnBoundSharedMemoryWriter_1"}
    assert storage.load_current(result_key) is None
    assert record_dir.exists()


@pytest.mark.shared_memory
def test_column_bound_shared_array_processing_tool_upstream_change_reports_pending_upstream(
    tmp_path: Path,
) -> None:
    storage_path = tmp_path / "results"

    with Workflow(engine="direct", storage_path=storage_path) as wf:
        table = CountingTable()(value=4)
        wf.compute(ColumnBoundSharedMemoryWriter()(label=table["label"]))
        node_name = "ColumnBoundSharedMemoryWriter_1"
        prior_result_key = _planned_result_key(wf, node_name)

    assert Storage(storage_path).load_current(prior_result_key) is not None
    with Workflow(engine="direct", storage_path=storage_path) as wf:
        table = CountingTable()(value=5)
        node = ColumnBoundSharedMemoryWriter()(label=table["label"])
        plan = wf.plan()

    assert plan[node.name].status is NodePlanStatus.PENDING_UPSTREAM
    assert plan[node.name].pending_upstreams == ("CountingTable_1",)
    assert plan[node.name].final_result_key is None


@pytest.mark.shared_memory
def test_column_bound_shared_array_plan_and_invalidate_do_not_rehydrate_shared_memory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage_path = tmp_path / "results"

    with Workflow(engine="direct", storage_path=storage_path) as wf:
        table = CountingTable()(value=4)
        wf.compute(ColumnBoundSharedMemoryWriter()(label=table["label"]))

    def fail_create_shared_output(*args, **kwargs):
        raise AssertionError("planning should not create shared-memory segments")

    monkeypatch.setattr(
        "bioimageflow_core.shm.create_shared_output", fail_create_shared_output
    )

    with Workflow(engine="direct", storage_path=storage_path) as wf:
        table = CountingTable()(value=4)
        node = ColumnBoundSharedMemoryWriter()(label=table["label"])
        plan = wf.plan()

    assert plan[node.name].status is NodePlanStatus.CACHED

    with Workflow(engine="direct", storage_path=storage_path) as wf:
        table = CountingTable()(value=4)
        node = ColumnBoundSharedMemoryWriter()(label=table["label"])
        cleared = wf.invalidate([node.name])

    assert _invalidated_node_names(cleared) == {"ColumnBoundSharedMemoryWriter_1"}


@pytest.mark.shared_memory
def test_column_bound_shared_array_processing_tool_publishes_one_shared_asset_per_row(
    tmp_path: Path,
) -> None:
    from bioimageflow_core.shm import open_shared_array

    storage_path = tmp_path / "results"
    ColumnBoundSharedMemoryWriter.executions = 0

    with Workflow(engine="direct", storage_path=storage_path) as wf:
        table = MultiRowTable()()
        node = ColumnBoundSharedMemoryWriter()(label=table["label"])
        first = wf.compute(node)
        result_key = _planned_result_key(wf, node.name)

    assert ColumnBoundSharedMemoryWriter.executions == 2
    storage = Storage(storage_path)
    pointer = storage.load_current(result_key)
    assert pointer is not None
    record_dir = storage.result_dir(result_key) / "records" / pointer.record_id
    manifest = json.loads((record_dir / "manifest.json").read_text())
    outputs = sorted(
        manifest["outputs"], key=lambda output: output["array"]["row_index"]
    )
    assert [output["array"]["row_index"] for output in outputs] == ["a", "b"]
    assert [output["array"]["column"] for output in outputs] == ["result", "result"]
    assert [output["array"]["shape"] for output in outputs] == [[2, 2], [2, 2]]
    assert all(output["asset_role"] == "shared_array" for output in outputs)
    assert all(output["path"].startswith("assets/shm/result_") for output in outputs)
    assert all((record_dir / output["path"]).exists() for output in outputs)

    stored = pd.read_parquet(record_dir / "dataframe.parquet")
    assert all(
        str(value).startswith("assets/shm/result_") for value in stored["result"]
    )
    assert set(first.index) == {"a", "b"}
    with open_shared_array(first.loc["a", "result"]) as array:
        assert int(array.sum()) == 20
    with open_shared_array(first.loc["b", "result"]) as array:
        assert int(array.sum()) == 16

    with Workflow(engine="direct", storage_path=storage_path) as wf:
        table = MultiRowTable()()
        second = wf.compute(ColumnBoundSharedMemoryWriter()(label=table["label"]))

    assert ColumnBoundSharedMemoryWriter.executions == 2
    with open_shared_array(second.loc["a", "result"]) as array:
        assert int(array.sum()) == 20
    with open_shared_array(second.loc["b", "result"]) as array:
        assert int(array.sum()) == 16


@pytest.mark.shared_memory
def test_column_bound_shared_array_processing_tool_invalidate_removes_corrupt_current(
    tmp_path: Path,
) -> None:
    storage_path = tmp_path / "results"

    with Workflow(engine="direct", storage_path=storage_path) as wf:
        table = CountingTable()(value=4)
        node = ColumnBoundSharedMemoryWriter()(label=table["label"])
        wf.compute(node)
        result_key = _planned_result_key(wf, node.name)

    storage = Storage(storage_path)
    pointer = storage.load_current(result_key)
    assert pointer is not None
    record_dir = storage.result_dir(result_key) / "records" / pointer.record_id
    current_path = storage.result_dir(result_key) / "current.json"
    current_path.write_text("{not json")

    with Workflow(engine="direct", storage_path=storage_path) as wf:
        table = CountingTable()(value=4)
        node = ColumnBoundSharedMemoryWriter()(label=table["label"])
        cleared = wf.invalidate([node.name])

    assert _invalidated_node_names(cleared) == {"ColumnBoundSharedMemoryWriter_1"}
    assert not current_path.exists()
    assert record_dir.exists()


@pytest.mark.shared_memory
def test_column_bound_shared_array_processing_tool_missing_asset_raises_cache_corruption(
    tmp_path: Path,
) -> None:
    storage_path = tmp_path / "results"

    with Workflow(engine="direct", storage_path=storage_path) as wf:
        table = CountingTable()(value=4)
        node = ColumnBoundSharedMemoryWriter()(label=table["label"])
        wf.compute(node)
        result_key = _planned_result_key(wf, node.name)

    storage = Storage(storage_path)
    pointer = storage.load_current(result_key)
    assert pointer is not None
    record_dir = storage.result_dir(result_key) / "records" / pointer.record_id
    manifest = json.loads((record_dir / "manifest.json").read_text())
    (record_dir / manifest["outputs"][0]["path"]).unlink()

    with Workflow(engine="direct", storage_path=storage_path) as wf:
        table = CountingTable()(value=4)
        with pytest.raises(CacheCorruptionError, match="Record asset is missing"):
            wf.compute(ColumnBoundSharedMemoryWriter()(label=table["label"]))
