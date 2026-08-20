"""Recursive selected-provider identity and non-reusable execution tests."""

import json
from pathlib import Path
import re

import pandas as pd
import pytest

from bioimageflow import (
    DataFrameTool,
    NodePlanStatus,
    ProgressEvent,
    SequentialEngine,
    Workflow,
)
from bioimageflow.backends import DirectBackend, ProcessingDispatch
from bioimageflow.storage import Storage
from bioimageflow_core import (
    Arguments,
    EnvironmentSpec,
    ExecutionContext,
    IOModel,
    ProcessingTool,
    RowConsumption,
    Template,
)


class ProviderTable(DataFrameTool):
    accepts_upstream = False

    class Inputs(IOModel):
        value: int = 4

    class Outputs(IOModel):
        value: int

    def transform(self, df: pd.DataFrame, arguments: Arguments) -> pd.DataFrame:
        return pd.DataFrame({"value": [arguments.value]}, index=["row"])


class FirstColumnValue(DataFrameTool):
    class Inputs(IOModel):
        pass

    class Outputs(IOModel):
        total: int

    def transform(self, df: pd.DataFrame, arguments: Arguments) -> pd.DataFrame:
        frame = pd.DataFrame(df)
        return pd.DataFrame(
            {"total": frame.iloc[:, 0] * 2},
            index=frame.index,
        )


class ValueAssetWriter(ProcessingTool):
    row_consumption = RowConsumption.MAPPED
    environment = EnvironmentSpec(name="recursive_value_writer", dependencies={})

    class Inputs(IOModel):
        value: int

    class Outputs(IOModel):
        asset: Path = Template("value_{row_index}.txt")
        copied: int

    def process_row(
        self,
        arguments: Arguments,
        *,
        context: ExecutionContext | None = None,
    ):
        assert context is not None
        asset = Path(arguments.asset)
        asset.write_text(str(arguments.value))
        return self.Outputs(asset=asset, copied=arguments.value)


class FailingValueAssetWriter(ValueAssetWriter):
    row_consumption = RowConsumption.MAPPED

    def process_row(
        self,
        arguments: Arguments,
        *,
        context: ExecutionContext | None = None,
    ):
        assert context is not None
        Path(arguments.asset).write_text("partial")
        raise RuntimeError("transient failure")


class RemoveProviderSelection(DataFrameTool):
    class Inputs(IOModel):
        storage_path: Path
        provider_name: str

    class Outputs(IOModel):
        value: int

    def transform(self, df: pd.DataFrame, arguments: Arguments) -> pd.DataFrame:
        results_root = Path(arguments.storage_path) / "cache" / "v1" / "results"
        matches = 0
        for metadata_path in results_root.glob("*/*/rk_*/result.json"):
            metadata = json.loads(metadata_path.read_text())
            if metadata.get("node") != arguments.provider_name:
                continue
            current_path = metadata_path.parent / "current.json"
            if current_path.exists():
                current_path.unlink()
                matches += 1
        if matches != 1:
            raise AssertionError(
                f"Expected one selected provider record, removed {matches}."
            )
        return pd.DataFrame(df)


class CapturingDirectBackend(DirectBackend):
    def __init__(self) -> None:
        self.requests: list[ProcessingDispatch] = []

    def dispatch(self, engine, request):
        self.requests.append(request)
        return super().dispatch(engine, request)


def _build_named_output_workflow(
    storage_path: Path,
    *,
    output_name: str,
) -> tuple[Workflow, object, object]:
    child = Workflow(name="child", storage_path=storage_path, engine="direct")
    with child:
        provider = ProviderTable()(value=4, name="provider")
        child.output(output_name, provider["value"], id="stable-output")

    parent = Workflow(name="parent", storage_path=storage_path, engine="direct")
    with parent:
        nested = child(name="nested")
        whole = FirstColumnValue()(nested, name="whole")
        column = ValueAssetWriter()(
            value=nested[output_name],
            name="column",
        )
    return parent, whole, column


def _build_non_reusable_workflow(
    storage_path: Path,
    *,
    on_progress=None,
    writer_tool: ProcessingTool | None = None,
) -> tuple[Workflow, object, object]:
    child = Workflow(name="child", storage_path=storage_path, engine="direct")
    with child:
        provider = ProviderTable()(value=7, name="provider")
        RemoveProviderSelection()(
            provider,
            storage_path=storage_path,
            provider_name="nested/provider",
            name="remove_selection",
        )
        child.output("value", provider["value"], id="stable-output")

    parent = Workflow(
        name="parent",
        storage_path=storage_path,
        engine="direct",
        on_progress=on_progress,
    )
    with parent:
        nested = child(name="nested")
        consumer = FirstColumnValue()(nested, name="consumer")
        writer = (writer_tool or ValueAssetWriter())(
            value=consumer["total"],
            name="writer",
        )
        parent.output("asset", writer["asset"], id="asset-output")
        parent.output("copied", writer["copied"], id="copied-output")
    return parent, consumer, writer


def test_recursive_planning_waits_for_selected_real_providers(
    tmp_path: Path,
) -> None:
    storage_path = tmp_path / "storage"
    workflow, whole, column = _build_named_output_workflow(
        storage_path,
        output_name="value",
    )

    plan = workflow.plan()

    assert plan[whole.name].status is NodePlanStatus.PENDING_UPSTREAM
    assert plan[whole.name].final_result_key is None
    assert plan[whole.name].pending_upstreams == ("nested",)
    assert plan[column.name].status is NodePlanStatus.PENDING_UPSTREAM
    assert plan[column.name].final_result_key is None
    assert plan[column.name].pending_upstreams == ("nested",)
    assert not (storage_path / "cache" / "v1" / "transient").exists()


def test_whole_boundary_identity_tracks_public_names_but_column_identity_uses_ids(
    tmp_path: Path,
) -> None:
    storage_path = tmp_path / "storage"
    first, first_whole, first_column = _build_named_output_workflow(
        storage_path,
        output_name="value",
    )
    first.compute(first_whole, first_column)
    first_plan = first.plan()

    renamed, renamed_whole, renamed_column = _build_named_output_workflow(
        storage_path,
        output_name="renamed",
    )
    renamed_plan = renamed.plan()

    assert (
        renamed_plan[renamed_whole.name].final_result_key
        != first_plan[first_whole.name].final_result_key
    )
    assert (
        renamed_plan[renamed_column.name].final_result_key
        == first_plan[first_column.name].final_result_key
    )
    assert renamed_plan[renamed_column.name].status is NodePlanStatus.CACHED


def test_reusable_processing_dispatch_correlates_invocation_and_attempt(
    tmp_path: Path,
) -> None:
    storage_path = tmp_path / "storage"
    workflow, _whole, column = _build_named_output_workflow(
        storage_path,
        output_name="value",
    )
    engine = SequentialEngine()
    backend = CapturingDirectBackend()
    engine._backend = backend

    workflow.compute(column, engine=engine)

    [request] = backend.requests
    assert re.fullmatch(r"inv_[0-9a-f]{32}", request.invocation_id)
    assert request.cache_attempt_id is not None
    assert re.fullmatch(r"att_[0-9a-f]{32}", request.cache_attempt_id)
    result_key = workflow.plan()[column.name].final_result_key
    assert result_key is not None
    pointer = Storage(storage_path).load_current(result_key)
    assert pointer is not None
    assert pointer.attempt_id == request.cache_attempt_id


def test_missing_provider_selection_propagates_non_reusable_execution(
    tmp_path: Path,
) -> None:
    storage_path = tmp_path / "storage"
    events: list[ProgressEvent] = []
    workflow, consumer, writer = _build_non_reusable_workflow(
        storage_path,
        on_progress=events.append,
    )
    engine = SequentialEngine()
    backend = CapturingDirectBackend()
    engine._backend = backend

    result = workflow.compute(engine=engine)

    assert result.loc["row", "copied"] == 14
    asset = Path(result.loc["row", "asset"])
    assert asset.read_text() == "14"
    invocation_dirs = list(
        (
            storage_path
            / "cache"
            / "v1"
            / "transient"
            / "runs"
        ).glob("run_*/nodes/writer/inv_*")
    )
    assert len(invocation_dirs) == 1
    invocation_dir = invocation_dirs[0]
    assert asset.parent == invocation_dir / "assets"
    invocation = json.loads((invocation_dir / "invocation.json").read_text())
    assert invocation["status"] == "succeeded"
    assert invocation["invocation_id"] == invocation_dir.name
    assert invocation["node_key"] == "writer"
    assert invocation["engine"] == "direct:parallel"
    [request] = backend.requests
    assert request.invocation_id == invocation["invocation_id"]
    assert request.cache_attempt_id is None

    metadata_nodes = {
        json.loads(path.read_text())["node"]
        for path in (
            storage_path
            / "cache"
            / "v1"
            / "results"
        ).glob("*/*/rk_*/result.json")
    }
    assert consumer.name not in metadata_nodes
    assert writer.name not in metadata_nodes

    run_id = invocation["run_id"]
    assert not (
        storage_path
        / "views"
        / "runs"
        / run_id
        / "nodes"
        / consumer.name
        / "result.json"
    ).exists()
    assert not (
        storage_path
        / "views"
        / "runs"
        / run_id
        / "nodes"
        / writer.name
        / "result.json"
    ).exists()
    writer_events = [event for event in events if event.node_name == writer.name]
    assert {event.status for event in writer_events} >= {
        "started",
        "row_complete",
        "completed",
    }
    assert all(event.result_key is None for event in writer_events)
    assert all(event.record_id is None for event in writer_events)

    plan = workflow.plan()
    assert plan[consumer.name].status is NodePlanStatus.PENDING_UPSTREAM
    assert plan[consumer.name].final_result_key is None
    assert plan[writer.name].status is NodePlanStatus.PENDING_UPSTREAM
    assert plan[writer.name].final_result_key is None


def test_failed_non_reusable_processing_marks_transient_diagnostics(
    tmp_path: Path,
) -> None:
    storage_path = tmp_path / "storage"
    events: list[ProgressEvent] = []
    workflow, _consumer, writer = _build_non_reusable_workflow(
        storage_path,
        on_progress=events.append,
        writer_tool=FailingValueAssetWriter(),
    )

    with pytest.raises(RuntimeError, match="transient failure"):
        workflow.compute()

    [invocation_dir] = list(
        (
            storage_path
            / "cache"
            / "v1"
            / "transient"
            / "runs"
        ).glob("run_*/nodes/writer/inv_*")
    )
    invocation = json.loads((invocation_dir / "invocation.json").read_text())
    failure = json.loads((invocation_dir / "failed.json").read_text())
    assert invocation["status"] == "failed"
    assert failure["type"] == "RuntimeError"
    assert failure["message"] == "transient failure"
    failed = [
        event
        for event in events
        if event.node_name == writer.name and event.status == "failed"
    ]
    assert failed
    assert failed[-1].result_key is None
    assert failed[-1].record_id is None
