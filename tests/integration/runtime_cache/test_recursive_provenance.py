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
from bioimageflow.storage import CacheCorruptionError, Storage
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


def test_removed_current_pointer_preserves_admitted_provider_record(
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
    assert Path(result.loc["row", "asset"]).read_text() == "14"
    [request] = backend.requests
    assert request.cache_attempt_id is not None
    assert not (storage_path / "cache" / "v1" / "transient").exists()
    storage = Storage(storage_path)
    for name in (consumer.name, writer.name):
        [completed] = [
            e for e in events if e.node_name == name and e.status == "completed"
        ]
        assert completed.result_key is not None and completed.record_id is not None
        manifest = storage.load_record_manifest(
            completed.result_key, completed.record_id
        )
        assert manifest.record_id == completed.record_id
    provider_metadata = [
        p
        for p in (storage.cache_root / "results").glob("*/*/rk_*/result.json")
        if json.loads(p.read_text())["node"] == "nested/provider"
    ]
    assert len(provider_metadata) == 1
    assert not (provider_metadata[0].parent / "current.json").exists()
    [writer_event] = [
        e for e in events if e.node_name == writer.name and e.status == "completed"
    ]
    assert writer_event.result_key is not None
    attempt = json.loads(
        (
            storage.result_dir(writer_event.result_key)
            / "attempts"
            / request.cache_attempt_id
            / "attempt.json"
        ).read_text()
    )
    assert attempt["status"] == "succeeded"
    assert attempt["invocation_id"] == request.invocation_id


def test_failed_processing_retains_admitted_provider_and_attempt_diagnostics(
    tmp_path: Path,
) -> None:
    storage_path = tmp_path / "storage"
    events: list[ProgressEvent] = []
    workflow, _consumer, writer = _build_non_reusable_workflow(
        storage_path,
        on_progress=events.append,
        writer_tool=FailingValueAssetWriter(),
    )
    engine = SequentialEngine()
    backend = CapturingDirectBackend()
    engine._backend = backend

    with pytest.raises(RuntimeError, match="transient failure"):
        workflow.compute(engine=engine)

    [request] = backend.requests
    assert request.cache_attempt_id is not None
    failed = [e for e in events if e.node_name == writer.name and e.status == "failed"]
    assert failed and failed[-1].record_id is None
    storage = Storage(storage_path)
    [attempt_path] = list(
        (storage.cache_root / "results").glob(
            f"*/*/rk_*/attempts/{request.cache_attempt_id}/attempt.json"
        )
    )
    attempt = json.loads(attempt_path.read_text())
    assert attempt["status"] == "failed"
    assert attempt["error_type"] == "RuntimeError"
    assert attempt["invocation_id"] == request.invocation_id
    assert storage.load_current(attempt["result_key"]) is None
    assert not (storage_path / "cache" / "v1" / "transient").exists()


def test_missing_provider_record_refuses_before_downstream_dispatch(
    tmp_path: Path,
) -> None:
    storage_path = tmp_path / "storage"
    workflow, _whole, writer = _build_named_output_workflow(
        storage_path, output_name="value"
    )
    workflow.compute(writer)
    storage = Storage(storage_path)
    [metadata] = [
        p
        for p in (storage.cache_root / "results").glob("*/*/rk_*/result.json")
        if json.loads(p.read_text())["node"] == "nested/provider"
    ]
    pointer = json.loads((metadata.parent / "current.json").read_text())
    record = metadata.parent / "records" / pointer["record_id"]
    (record / "dataframe.parquet").unlink()
    downstream_pointer = [
        p.read_bytes()
        for p in (storage.cache_root / "results").glob("*/*/rk_*/current.json")
        if p.parent != metadata.parent
    ]
    engine = SequentialEngine()
    backend = CapturingDirectBackend()
    engine._backend = backend

    with pytest.raises(CacheCorruptionError):
        workflow.compute(writer, engine=engine)

    assert backend.requests == []
    assert [
        p.read_bytes()
        for p in (storage.cache_root / "results").glob("*/*/rk_*/current.json")
        if p.parent != metadata.parent
    ] == downstream_pointer
