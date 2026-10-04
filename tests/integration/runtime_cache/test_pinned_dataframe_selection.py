"""A consumed immutable frame retains its selection across pointer changes."""

import json
from pathlib import Path

import pandas as pd
import pytest

from bioimageflow import ColumnNotFoundError, ProgressEvent, Workflow, WorkflowExecutionContext, export_outputs
from bioimageflow.storage import Storage
from tests.testkit.runtime_cache import (
    CountingTable,
    DoubleValue,
    _force_current_record,
    _write_manual_dataframe_record,
)


class ExplicitEmptyTable(CountingTable):
    @classmethod
    def resolve_outputs(cls, config):
        return {}


def test_dynamic_dataframe_columns_and_explicit_empty_resolution_are_distinct(
    tmp_path: Path,
) -> None:
    with Workflow(engine="direct", storage_path=tmp_path / "results") as workflow:
        dynamic = CountingTable()(value=4)
        dynamic["value"]
        explicit_empty = ExplicitEmptyTable()(value=4)
        with pytest.raises(ColumnNotFoundError, match="value"):
            explicit_empty["value"]
        assert workflow.compute(dynamic)["value"].tolist() == [4]


def test_workflow_keeps_consumed_record_when_current_changes_after_frame_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage_path = tmp_path / "results"
    seed_context = WorkflowExecutionContext()
    with Workflow(engine="direct", storage_path=storage_path) as workflow:
        source = CountingTable()(value=4, name="source")
        seed = workflow.compute(source, run_context=seed_context)
    assert seed["value"].tolist() == [4]
    [seed_outcome] = seed_context.execution_outcomes
    assert seed_outcome.result_key is not None
    assert seed_outcome.record_id is not None
    result_key, record_a = seed_outcome.result_key, seed_outcome.record_id
    storage = Storage(storage_path)
    frame_a_path = storage.result_dir(result_key) / "records" / record_a / "dataframe.parquet"
    record_b = _write_manual_dataframe_record(
        storage,
        result_key,
        pd.DataFrame({"value": [99], "label": ["alternate"]}, index=["row"]),
    )
    assert record_b != record_a
    original_read = pd.read_parquet
    switched = False

    def read_then_switch(path, *args, **kwargs):
        nonlocal switched
        frame = original_read(path, *args, **kwargs)
        if not switched and Path(path) == frame_a_path:
            assert frame["value"].tolist() == [4]
            _force_current_record(storage, result_key, record_b)
            switched = True
        return frame

    monkeypatch.setattr(pd, "read_parquet", read_then_switch)
    context = WorkflowExecutionContext()
    events: list[ProgressEvent] = []
    with Workflow(engine="direct", storage_path=storage_path, on_progress=events.append) as workflow:
        source = CountingTable()(value=4, name="source")
        derived = DoubleValue()(source, name="derived")
        result = workflow.compute(derived, run_context=context)

    assert switched, "the pointer must change after the real cached A frame was read"
    assert result["value"].tolist() == [4]
    assert result["double"].tolist() == [8]
    pointer = storage.load_current(result_key)
    assert pointer is not None and pointer.record_id == record_b
    outcomes = {item.node_key: item for item in context.execution_outcomes}
    assert (outcomes["source"].result_key, outcomes["source"].record_id) == (result_key, record_a)
    selected_events = [event for event in events if event.node_name == "source" and event.status == "cached"]
    assert selected_events
    assert all((event.result_key, event.record_id) == (result_key, record_a) for event in selected_events)
    run_node = storage_path / "views" / "runs" / context.run_id / "nodes" / "source"
    run_result = json.loads((run_node / "result.json").read_text())
    assert (run_result["result_key"], run_result["record_id"]) == (result_key, record_a)
    destination = tmp_path / "export"
    export_outputs(storage_path, destination=destination, scope="runs", run_id=context.run_id, mode="copy")
    exported_run = destination / "runs" / context.run_id / "nodes"
    assert pd.read_parquet(exported_run / "source" / "outputs" / "dataframe.parquet")["value"].tolist() == [4]
    provenance = json.loads((exported_run / "derived" / "outputs" / "provenance.json").read_text())
    [provider] = provenance["computation"]["inputs"]["argument_0"]["providers"]
    assert provider["provider"] == {"node_key": "source", "result_key": result_key, "record_id": record_a}
