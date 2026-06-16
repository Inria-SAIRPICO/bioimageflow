"""Runtime integration tests for the v1 output/cache storage contract."""

from __future__ import annotations

import json
import hashlib
from pathlib import Path
from typing import Annotated

import pandas as pd
import pytest

from bioimageflow import NodePlanStatus, Workflow
from bioimageflow.dataframe_tool import DataFrameTool
from bioimageflow.cache import dataframe_v1_result_key
from bioimageflow.storage_v1 import CacheCorruptionError, StorageV1, make_record_id
from bioimageflow_core import IOModel
from bioimageflow_core.types import ImageSpec, Semantic


class CountingTable(DataFrameTool):
    display_name = "Counting Table"
    accepts_upstream = False
    executions = 0

    class Inputs(IOModel):
        value: int = 1

    def transform(self, df: pd.DataFrame, arguments) -> pd.DataFrame:
        type(self).executions += 1
        return pd.DataFrame({"value": [arguments.value], "label": [f"v{arguments.value}"]}, index=["row"])


class DoubleValue(DataFrameTool):
    display_name = "Double Value"
    executions = 0

    def transform(self, df: pd.DataFrame, arguments) -> pd.DataFrame:
        type(self).executions += 1
        result = df.copy()
        result["double"] = result["value"] * 2
        return result


class PathTable(DataFrameTool):
    display_name = "Path Table"
    accepts_upstream = False
    executions = 0

    class Inputs(IOModel):
        path: Path

    class Outputs(IOModel):
        path: Annotated[Path, ImageSpec(semantics={Semantic.INTENSITY})]

    def transform(self, df: pd.DataFrame, arguments) -> pd.DataFrame:
        type(self).executions += 1
        return pd.DataFrame({"path": [arguments.path]}, index=["row"])


def _current_pointer_files(storage_path: Path) -> list[Path]:
    return sorted((storage_path / "cache" / "v1" / "results").glob("*/*/rk_*/current.json"))


def _parquet_digest(df: pd.DataFrame, path: Path) -> str:
    df.to_parquet(path, index=True)
    return f"sha256:{hashlib.sha256(path.read_bytes()).hexdigest()}"


def test_dataframe_tool_compute_publishes_v1_record_and_uses_cache_hit(tmp_path: Path) -> None:
    storage_path = tmp_path / "results"
    CountingTable.executions = 0

    with Workflow(storage_path=storage_path) as wf:
        table = CountingTable()(value=7)
        first = wf.compute(table)

    current_files = _current_pointer_files(storage_path)
    assert len(current_files) == 1
    current = json.loads(current_files[0].read_text())
    result_key = current["result_key"]
    record_id = current["record_id"]
    storage = StorageV1(storage_path)
    pointer = storage.load_current(result_key)
    assert pointer is not None
    assert pointer.record_id == record_id
    assert (storage.result_dir(result_key) / "records" / record_id / "dataframe.parquet").exists()
    assert not (storage_path / "data" / "CountingTable_1").exists()

    events: list[tuple[str, str]] = []
    with Workflow(storage_path=storage_path, on_progress=lambda event: events.append((event.node_name, event.status))) as wf:
        table = CountingTable()(value=7)
        second = wf.compute(table)

    pd.testing.assert_frame_equal(first, second)
    assert CountingTable.executions == 1
    assert ("CountingTable_1", "cached") in events
    assert _current_pointer_files(storage_path) == current_files


def test_dataframe_tool_parameter_change_creates_distinct_v1_result_without_legacy_data(tmp_path: Path) -> None:
    storage_path = tmp_path / "results"

    with Workflow(storage_path=storage_path) as wf:
        wf.compute(CountingTable()(value=1))
    with Workflow(storage_path=storage_path) as wf:
        wf.compute(CountingTable()(value=2))

    current_files = _current_pointer_files(storage_path)
    assert len(current_files) == 2
    assert {json.loads(path.read_text())["result_key"] for path in current_files}
    assert not (storage_path / "data").exists()


def test_chained_dataframe_tools_use_v1_cache_for_each_node(tmp_path: Path) -> None:
    storage_path = tmp_path / "results"
    CountingTable.executions = 0
    DoubleValue.executions = 0

    with Workflow(storage_path=storage_path) as wf:
        table = CountingTable()(value=4)
        doubled = DoubleValue()(table)
        first = wf.compute(doubled)

    assert first.loc["row", "double"] == 8
    assert len(_current_pointer_files(storage_path)) == 2

    events: list[tuple[str, str]] = []
    with Workflow(storage_path=storage_path, on_progress=lambda event: events.append((event.node_name, event.status))) as wf:
        table = CountingTable()(value=4)
        doubled = DoubleValue()(table)
        second = wf.compute(doubled)

    pd.testing.assert_frame_equal(first, second)
    assert CountingTable.executions == 1
    assert DoubleValue.executions == 1
    assert ("CountingTable_1", "cached") in events
    assert ("DoubleValue_1", "cached") in events


def test_dataframe_tool_plan_reports_cached_from_v1_current(tmp_path: Path) -> None:
    storage_path = tmp_path / "results"

    with Workflow(storage_path=storage_path) as wf:
        wf.compute(CountingTable()(value=3))

    with Workflow(storage_path=storage_path) as wf:
        table = CountingTable()(value=3)
        plan = wf.plan()

    assert plan[table.name].status is NodePlanStatus.CACHED


def test_dataframe_tool_corrupt_v1_current_raises(tmp_path: Path) -> None:
    storage_path = tmp_path / "results"

    with Workflow(storage_path=storage_path) as wf:
        wf.compute(CountingTable()(value=5))
    [current_path] = _current_pointer_files(storage_path)
    current_path.write_text("{not json")

    with Workflow(storage_path=storage_path) as wf:
        table = CountingTable()(value=5)
        with pytest.raises(CacheCorruptionError):
            wf.compute(table)


def test_dataframe_tool_path_outputs_are_normalized_after_publish_hit_and_steps(tmp_path: Path) -> None:
    storage_path = tmp_path / "results"
    input_path = tmp_path / "image.tif"
    input_path.write_text("image")
    PathTable.executions = 0

    with Workflow(storage_path=storage_path) as wf:
        first = wf.compute(PathTable()(path=input_path))

    with Workflow(storage_path=storage_path) as wf:
        second = wf.compute(PathTable()(path=input_path))

    with Workflow(storage_path=storage_path) as wf:
        node = PathTable()(path=input_path)
        step = next(wf.compute_steps(node))
        stepped = step.execute()

    assert PathTable.executions == 1
    assert first.loc["row", "path"] == str(input_path)
    assert second.loc["row", "path"] == str(input_path)
    assert stepped.loc["row", "path"] == str(input_path)
    assert type(first.loc["row", "path"]) is type(second.loc["row", "path"]) is type(stepped.loc["row", "path"])


def test_dataframe_tool_invalidate_removes_v1_current_but_keeps_record(tmp_path: Path) -> None:
    storage_path = tmp_path / "results"
    CountingTable.executions = 0

    with Workflow(storage_path=storage_path) as wf:
        node = CountingTable()(value=9)
        wf.compute(node)
        node_name = node.name
        sig_hash = wf.plan()[node_name].sig_hash

    result_key = dataframe_v1_result_key(node_name, sig_hash)
    storage = StorageV1(storage_path)
    pointer = storage.load_current(result_key)
    assert pointer is not None
    record_dir = storage.result_dir(result_key) / "records" / pointer.record_id
    assert record_dir.exists()

    with Workflow(storage_path=storage_path) as wf:
        node = CountingTable()(value=9)
        cleared = wf.invalidate([node.name])

    assert cleared == {node_name}
    assert storage.load_current(result_key) is None
    assert record_dir.exists()

    with Workflow(storage_path=storage_path) as wf:
        wf.compute(CountingTable()(value=9))

    assert CountingTable.executions == 2


def test_dataframe_tool_invalidate_removes_corrupt_v1_current(tmp_path: Path) -> None:
    storage_path = tmp_path / "results"

    with Workflow(storage_path=storage_path) as wf:
        node = CountingTable()(value=10)
        wf.compute(node)
        node_name = node.name
        sig_hash = wf.plan()[node_name].sig_hash

    result_key = dataframe_v1_result_key(node_name, sig_hash)
    current_path = StorageV1(storage_path).result_dir(result_key) / "current.json"
    current_path.write_text("{not json")

    with Workflow(storage_path=storage_path) as wf:
        node = CountingTable()(value=10)
        cleared = wf.invalidate([node.name])

    assert cleared == {node_name}
    assert not current_path.exists()


def test_dataframe_tool_corrupt_v1_dataframe_file_raises_cache_corruption(tmp_path: Path) -> None:
    storage_path = tmp_path / "results"

    with Workflow(storage_path=storage_path) as wf:
        wf.compute(CountingTable()(value=11))
    [current_path] = _current_pointer_files(storage_path)
    current = json.loads(current_path.read_text())
    record_dir = StorageV1(storage_path).result_dir(current["result_key"]) / "records" / current["record_id"]
    (record_dir / "dataframe.parquet").write_text("not parquet")

    with Workflow(storage_path=storage_path) as wf:
        with pytest.raises(CacheCorruptionError):
            wf.compute(CountingTable()(value=11))


def test_dataframe_tool_publish_rejects_symlinked_record_directory_before_writing(tmp_path: Path) -> None:
    from bioimageflow.cache import dataframe_v1_publish

    storage_path = tmp_path / "results"
    node_name = "CountingTable_1"
    sig_hash = "sig"
    df = pd.DataFrame({"value": [1]})
    result_key = dataframe_v1_result_key(node_name, sig_hash)
    dataframe_digest = _parquet_digest(df, tmp_path / "expected.parquet")
    parquet_content = b"PAR1"
    record_id = make_record_id(
        {
            "schema": "bioimageflow.cache.record.v1",
            "result_key": result_key,
            "dataframe": {
                "path": "dataframe.parquet",
                "digest": dataframe_digest,
            },
            "outputs": [],
        }
    )
    records_dir = StorageV1(storage_path).result_dir(result_key) / "records"
    records_dir.mkdir(parents=True)
    outside = tmp_path / "outside-record"
    outside.mkdir()
    (outside / "dataframe.parquet").write_bytes(parquet_content)
    (records_dir / record_id).symlink_to(outside)

    with pytest.raises(CacheCorruptionError):
        dataframe_v1_publish(storage_path, node_name, sig_hash, df)
    assert (outside / "dataframe.parquet").read_bytes() == parquet_content


def test_dataframe_tool_publish_rejects_symlinked_records_directory_before_writing(tmp_path: Path) -> None:
    from bioimageflow.cache import dataframe_v1_publish

    storage_path = tmp_path / "results"
    node_name = "CountingTable_1"
    sig_hash = "sig"
    result_key = dataframe_v1_result_key(node_name, sig_hash)
    records_dir = StorageV1(storage_path).result_dir(result_key) / "records"
    records_dir.parent.mkdir(parents=True)
    outside = tmp_path / "outside-records"
    outside.mkdir()
    records_dir.symlink_to(outside)

    with pytest.raises(CacheCorruptionError):
        dataframe_v1_publish(storage_path, node_name, sig_hash, pd.DataFrame({"value": [1]}))
    assert list(outside.iterdir()) == []
