"""Focused tests split from ``tests/integration/runtime_cache/test_dataframe_and_runs.py``."""

import json


from pathlib import Path


import pandas as pd

import pytest

from bioimageflow import NodePlanStatus, Workflow


from bioimageflow.cache import (
    dataframe_result_key,
)

from bioimageflow.storage import (
    CacheCorruptionError,
    Storage,
    make_record_id,
)


from tests.testkit.runtime_cache import (
    CountingTable,
    DoubleValue,
    PathTable,
    _current_pointer_files,
    _force_current_record,
    _invalidated_node_names,
    _parquet_digest,
    _planned_result_key,
    _selection_for,
    _write_manual_dataframe_record,
)


def test_dataframe_tool_parameter_change_creates_distinct_result_record(
    tmp_path: Path,
) -> None:
    storage_path = tmp_path / "results"

    with Workflow(engine="direct", storage_path=storage_path) as wf:
        wf.compute(CountingTable()(value=1))
    with Workflow(engine="direct", storage_path=storage_path) as wf:
        wf.compute(CountingTable()(value=2))

    current_files = _current_pointer_files(storage_path)
    assert len(current_files) == 2
    assert {json.loads(path.read_text())["result_key"] for path in current_files}


def test_chained_dataframe_tools_use_current_cache_for_each_node(
    tmp_path: Path,
) -> None:
    storage_path = tmp_path / "results"
    CountingTable.executions = 0
    DoubleValue.executions = 0

    with Workflow(engine="direct", storage_path=storage_path) as wf:
        table = CountingTable()(value=4)
        doubled = DoubleValue()(table)
        first = wf.compute(doubled)

    assert first.loc["row", "double"] == 8
    assert len(_current_pointer_files(storage_path)) == 2

    events: list[tuple[str, str]] = []
    with Workflow(
        engine="direct",
        storage_path=storage_path,
        on_progress=lambda event: events.append((event.node_name, event.status)),
    ) as wf:
        table = CountingTable()(value=4)
        doubled = DoubleValue()(table)
        second = wf.compute(doubled)

    pd.testing.assert_frame_equal(first, second)
    assert CountingTable.executions == 1
    assert DoubleValue.executions == 1
    assert ("CountingTable_1", "cached") in events
    assert ("DoubleValue_1", "cached") in events


def test_downstream_result_key_tracks_selected_upstream_record(tmp_path: Path) -> None:
    storage_path = tmp_path / "results"
    CountingTable.executions = 0
    DoubleValue.executions = 0

    with Workflow(engine="direct", storage_path=storage_path) as wf:
        table = CountingTable()(value=4)
        doubled = DoubleValue()(table)
        first = wf.compute(doubled)
        table_result_key = _planned_result_key(wf, table.name)
        first_downstream_key = _planned_result_key(wf, doubled.name)

    assert first.loc["row", "double"] == 8
    storage = Storage(storage_path)
    alternate_record_id = _write_manual_dataframe_record(
        storage,
        table_result_key,
        pd.DataFrame({"value": [99], "label": ["alternate"]}, index=["row"]),
    )
    _force_current_record(storage, table_result_key, alternate_record_id)

    with Workflow(engine="direct", storage_path=storage_path) as wf:
        table = CountingTable()(value=4)
        doubled = DoubleValue()(table)
        plan = wf.plan()
        assert plan[table.name].selected_record_id == alternate_record_id
        assert plan[doubled.name].final_result_key != first_downstream_key
        second = wf.compute(doubled)

    assert second.loc["row", "double"] == 198
    assert CountingTable.executions == 1
    assert DoubleValue.executions == 2


def test_dataframe_tool_plan_reports_cached_from_current(tmp_path: Path) -> None:
    storage_path = tmp_path / "results"

    with Workflow(engine="direct", storage_path=storage_path) as wf:
        wf.compute(CountingTable()(value=3))

    with Workflow(engine="direct", storage_path=storage_path) as wf:
        table = CountingTable()(value=3)
        plan = wf.plan()

    assert plan[table.name].status is NodePlanStatus.CACHED


def test_dataframe_tool_corrupt_current_raises(tmp_path: Path) -> None:
    storage_path = tmp_path / "results"

    with Workflow(engine="direct", storage_path=storage_path) as wf:
        wf.compute(CountingTable()(value=5))
    [current_path] = _current_pointer_files(storage_path)
    current_path.write_text("{not json")

    with Workflow(engine="direct", storage_path=storage_path) as wf:
        table = CountingTable()(value=5)
        with pytest.raises(CacheCorruptionError):
            wf.compute(table)


def test_dataframe_tool_path_outputs_are_normalized_after_publish_hit_and_steps(
    tmp_path: Path,
) -> None:
    storage_path = tmp_path / "results"
    input_path = tmp_path / "image.tif"
    input_path.write_text("image")
    PathTable.executions = 0

    with Workflow(engine="direct", storage_path=storage_path) as wf:
        first = wf.compute(PathTable()(path=input_path))

    with Workflow(engine="direct", storage_path=storage_path) as wf:
        second = wf.compute(PathTable()(path=input_path))

    with Workflow(engine="direct", storage_path=storage_path) as wf:
        node = PathTable()(path=input_path)
        step = next(wf.compute_steps(node))
        stepped = step.execute()

    assert PathTable.executions == 1
    assert first.loc["row", "path"] == str(input_path)
    assert second.loc["row", "path"] == str(input_path)
    assert stepped.loc["row", "path"] == str(input_path)
    assert (
        type(first.loc["row", "path"])
        is type(second.loc["row", "path"])
        is type(stepped.loc["row", "path"])
    )


def test_dataframe_tool_invalidate_removes_current_but_keeps_record(
    tmp_path: Path,
) -> None:
    storage_path = tmp_path / "results"
    CountingTable.executions = 0

    with Workflow(engine="direct", storage_path=storage_path) as wf:
        node = CountingTable()(value=9)
        wf.compute(node)
        node_name = node.name
        result_key = _planned_result_key(wf, node_name)

    storage = Storage(storage_path)
    pointer = storage.load_current(result_key)
    assert pointer is not None
    record_dir = storage.result_dir(result_key) / "records" / pointer.record_id
    assert record_dir.exists()

    with Workflow(engine="direct", storage_path=storage_path) as wf:
        node = CountingTable()(value=9)
        cleared = wf.invalidate([node.name])

    assert _invalidated_node_names(cleared) == {node_name}
    selection = _selection_for(cleared, node_name)
    assert selection.result_key == result_key
    assert selection.selected_record_id == pointer.record_id
    assert selection.status == "removed"
    assert storage.load_current(result_key) is None
    assert record_dir.exists()

    with Workflow(engine="direct", storage_path=storage_path) as wf:
        wf.compute(CountingTable()(value=9))

    assert CountingTable.executions == 2


def test_dataframe_tool_invalidate_removes_corrupt_current(tmp_path: Path) -> None:
    storage_path = tmp_path / "results"

    with Workflow(engine="direct", storage_path=storage_path) as wf:
        node = CountingTable()(value=10)
        wf.compute(node)
        node_name = node.name
        result_key = _planned_result_key(wf, node_name)

    current_path = Storage(storage_path).result_dir(result_key) / "current.json"
    current_path.write_text("{not json")

    with Workflow(engine="direct", storage_path=storage_path) as wf:
        node = CountingTable()(value=10)
        cleared = wf.invalidate([node.name])

    assert _invalidated_node_names(cleared) == {node_name}
    selection = _selection_for(cleared, node_name)
    assert selection.result_key == result_key
    assert selection.selected_record_id is None
    assert selection.status == "corrupt_removed"
    assert not current_path.exists()


def test_dataframe_tool_invalidate_removes_prior_current_selection(
    tmp_path: Path,
) -> None:
    storage_path = tmp_path / "results"

    with Workflow(engine="direct", storage_path=storage_path) as wf:
        node = CountingTable()(value=1)
        wf.compute(node)
        prior_result_key = _planned_result_key(wf, node.name)

    storage = Storage(storage_path)
    prior_pointer = storage.load_current(prior_result_key)
    assert prior_pointer is not None

    with Workflow(engine="direct", storage_path=storage_path) as wf:
        node = CountingTable()(value=2)
        cleared = wf.invalidate([node.name])

    assert _invalidated_node_names(cleared) == {"CountingTable_1"}
    selection = _selection_for(cleared, "CountingTable_1")
    assert selection.result_key == prior_result_key
    assert selection.selected_record_id == prior_pointer.record_id
    assert storage.load_current(prior_result_key) is None


def test_dataframe_tool_corrupt_dataframe_file_raises_cache_corruption(
    tmp_path: Path,
) -> None:
    storage_path = tmp_path / "results"

    with Workflow(engine="direct", storage_path=storage_path) as wf:
        wf.compute(CountingTable()(value=11))
    [current_path] = _current_pointer_files(storage_path)
    current = json.loads(current_path.read_text())
    record_dir = (
        Storage(storage_path).result_dir(current["result_key"])
        / "records"
        / current["record_id"]
    )
    (record_dir / "dataframe.parquet").write_text("not parquet")

    with Workflow(engine="direct", storage_path=storage_path) as wf:
        with pytest.raises(CacheCorruptionError):
            wf.compute(CountingTable()(value=11))


def test_dataframe_tool_publish_rejects_symlinked_record_directory_before_writing(
    tmp_path: Path,
) -> None:
    from bioimageflow.cache import dataframe_publish

    storage_path = tmp_path / "results"
    node_name = "CountingTable_1"
    sig_hash = "sig"
    df = pd.DataFrame({"value": [1]})
    result_key = dataframe_result_key(node_name, sig_hash)
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
    records_dir = Storage(storage_path).result_dir(result_key) / "records"
    records_dir.mkdir(parents=True)
    outside = tmp_path / "outside-record"
    outside.mkdir()
    (outside / "dataframe.parquet").write_bytes(parquet_content)
    (records_dir / record_id).symlink_to(outside)

    with pytest.raises(CacheCorruptionError):
        dataframe_publish(storage_path, node_name, sig_hash, df)
    assert (outside / "dataframe.parquet").read_bytes() == parquet_content


def test_dataframe_tool_publish_rejects_symlinked_records_directory_before_writing(
    tmp_path: Path,
) -> None:
    from bioimageflow.cache import dataframe_publish

    storage_path = tmp_path / "results"
    node_name = "CountingTable_1"
    sig_hash = "sig"
    result_key = dataframe_result_key(node_name, sig_hash)
    records_dir = Storage(storage_path).result_dir(result_key) / "records"
    records_dir.parent.mkdir(parents=True)
    outside = tmp_path / "outside-records"
    outside.mkdir()
    records_dir.symlink_to(outside)

    with pytest.raises(CacheCorruptionError):
        dataframe_publish(
            storage_path, node_name, sig_hash, pd.DataFrame({"value": [1]})
        )
    assert list(outside.iterdir()) == []
