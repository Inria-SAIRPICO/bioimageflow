"""Focused tests split from ``tests/integration/runtime_cache/test_shared_memory.py``."""

import json


from pathlib import Path


import pytest

from bioimageflow import NodePlanStatus, Workflow


from bioimageflow.storage import (
    CacheCorruptionError,
    Storage,
)


from bioimageflow_core.types import SharedArray

from tests.testkit.runtime_cache import (
    SourceFlexibleImageWriter,
    SourceSharedMemoryConsumer,
    SourceSharedMemoryWriter,
    _invalidated_node_names,
    _planned_result_key,
)


@pytest.mark.shared_memory
def test_source_shared_array_processing_tool_publishes_durable_asset_and_rehydrates_hits(
    tmp_path: Path,
) -> None:
    from bioimageflow_core.shm import open_shared_array

    storage_path = tmp_path / "results"
    SourceSharedMemoryWriter.executions = 0

    with Workflow(engine="direct", storage_path=storage_path) as wf:
        node = SourceSharedMemoryWriter()()
        first = wf.compute(node)
        result_key = _planned_result_key(wf, node.name)

    assert SourceSharedMemoryWriter.executions == 1
    assert isinstance(first.loc["0", "result"], SharedArray)
    first_ref = first.loc["0", "result"]
    with open_shared_array(first_ref) as array:
        assert array.shape == (2, 2)
        assert str(array.dtype) == "uint8"
        assert int(array.sum()) == 0

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
            "row_index": "0",
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
        second = wf.compute(SourceSharedMemoryWriter()())

    assert SourceSharedMemoryWriter.executions == 1
    assert isinstance(second.loc["0", "result"], SharedArray)
    second_ref = second.loc["0", "result"]
    assert second_ref.name != first_ref.name
    with open_shared_array(second_ref) as array:
        assert array.shape == (2, 2)
        assert str(array.dtype) == "uint8"
        assert int(array.sum()) == 0
    assert ("SourceSharedMemoryWriter_1", "cached") in events

    with Workflow(engine="direct", storage_path=storage_path) as wf:
        writer = SourceSharedMemoryWriter()()
        consumer = SourceSharedMemoryConsumer()(result=writer["result"])
        steps = list(wf.compute_steps(consumer))
        writer_step = next(step for step in steps if step.node_name == writer.name)
        assert writer_step.cached is True
        cached_writer_df = writer_step.execute()
        assert isinstance(cached_writer_df.loc["0", "result"], SharedArray)
        consumer_step = next(step for step in steps if step.node_name == consumer.name)
        consumer_df = consumer_step.execute()

    assert consumer_df.loc["0", "total"] == 0
    assert SourceSharedMemoryWriter.executions == 1

    with Workflow(engine="direct", storage_path=storage_path) as wf:
        node = SourceSharedMemoryWriter()()
        assert wf.plan()[node.name].status is NodePlanStatus.CACHED
        cleared = wf.invalidate([node.name])

    assert _invalidated_node_names(cleared) == {"SourceSharedMemoryWriter_1"}
    assert storage.load_current(result_key) is None
    assert record_dir.exists()


@pytest.mark.shared_memory
def test_source_shared_array_processing_tool_missing_asset_raises_cache_corruption(
    tmp_path: Path,
) -> None:
    storage_path = tmp_path / "results"

    with Workflow(engine="direct", storage_path=storage_path) as wf:
        node = SourceSharedMemoryWriter()()
        wf.compute(node)
        result_key = _planned_result_key(wf, node.name)

    storage = Storage(storage_path)
    pointer = storage.load_current(result_key)
    assert pointer is not None
    record_dir = storage.result_dir(result_key) / "records" / pointer.record_id
    manifest = json.loads((record_dir / "manifest.json").read_text())
    (record_dir / manifest["outputs"][0]["path"]).unlink()

    with Workflow(engine="direct", storage_path=storage_path) as wf:
        with pytest.raises(CacheCorruptionError, match="Record asset is missing"):
            wf.compute(SourceSharedMemoryWriter()())


@pytest.mark.shared_memory
def test_source_shared_array_processing_tool_parameter_change_reports_prior_selection_miss(
    tmp_path: Path,
) -> None:
    storage_path = tmp_path / "results"

    with Workflow(engine="direct", storage_path=storage_path) as wf:
        wf.compute(SourceSharedMemoryWriter()(value=0))

    with Workflow(engine="direct", storage_path=storage_path) as wf:
        node = SourceSharedMemoryWriter()(value=1)
        plan = wf.plan()

    assert plan[node.name].status is NodePlanStatus.PRIOR_SELECTION_MISS


def test_source_path_or_shared_array_output_handles_path_values(tmp_path: Path) -> None:
    storage_path = tmp_path / "results"
    SourceFlexibleImageWriter.executions = 0

    with Workflow(engine="direct", storage_path=storage_path) as wf:
        node = SourceFlexibleImageWriter()(as_shared_array=False)
        first = wf.compute(node)
        result_key = _planned_result_key(wf, node.name)

    result_path = Path(first.loc["0", "result"])
    assert result_path.name == "flex_0.txt"
    assert result_path.read_text() == "path-result"
    storage = Storage(storage_path)
    pointer = storage.load_current(result_key)
    assert pointer is not None
    record_dir = storage.result_dir(result_key) / "records" / pointer.record_id
    manifest = json.loads((record_dir / "manifest.json").read_text())
    assert manifest["outputs"][0]["kind"] == "owned_asset"
    assert manifest["outputs"][0]["path"] == "assets/flex_0.txt"

    with Workflow(engine="direct", storage_path=storage_path) as wf:
        second = wf.compute(SourceFlexibleImageWriter()(as_shared_array=False))

    assert Path(second.loc["0", "result"]) == result_path
    assert SourceFlexibleImageWriter.executions == 1


def test_source_path_or_shared_array_output_rejects_path_assets_under_shared_namespace(
    tmp_path: Path,
) -> None:
    storage_path = tmp_path / "results"

    with Workflow(engine="direct", storage_path=storage_path) as wf:
        node = SourceFlexibleImageWriter()(
            as_shared_array=False,
            output_templates={"result": "shm/path.txt"},
        )
        with pytest.raises(
            CacheCorruptionError, match="reserved for shared-array assets"
        ):
            wf.compute(node)


@pytest.mark.shared_memory
def test_source_path_or_shared_array_output_handles_shared_array_values(
    tmp_path: Path,
) -> None:
    from bioimageflow_core.shm import open_shared_array

    storage_path = tmp_path / "results"
    SourceFlexibleImageWriter.executions = 0

    with Workflow(engine="direct", storage_path=storage_path) as wf:
        node = SourceFlexibleImageWriter()(as_shared_array=True)
        first = wf.compute(node)
        result_key = _planned_result_key(wf, node.name)

    assert isinstance(first.loc["0", "result"], SharedArray)
    with open_shared_array(first.loc["0", "result"]) as array:
        assert int(array.sum()) == 4

    storage = Storage(storage_path)
    pointer = storage.load_current(result_key)
    assert pointer is not None
    record_dir = storage.result_dir(result_key) / "records" / pointer.record_id
    manifest = json.loads((record_dir / "manifest.json").read_text())
    assert manifest["outputs"][0]["asset_role"] == "shared_array"
    assert manifest["outputs"][0]["path"].startswith("assets/shm/result_")

    with Workflow(engine="direct", storage_path=storage_path) as wf:
        second = wf.compute(SourceFlexibleImageWriter()(as_shared_array=True))

    assert isinstance(second.loc["0", "result"], SharedArray)
    with open_shared_array(second.loc["0", "result"]) as array:
        assert int(array.sum()) == 4
    assert SourceFlexibleImageWriter.executions == 1
