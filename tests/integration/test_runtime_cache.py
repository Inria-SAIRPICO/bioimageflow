"""Runtime integration tests for the v1 output/cache storage contract."""

import json
import hashlib
from pathlib import Path
from typing import Annotated

import pandas as pd
import pytest

from bioimageflow import NodePlanStatus, ProgressEvent, Workflow
from bioimageflow.dataframe_tool import DataFrameTool
from bioimageflow.sub_workflow import SubWorkflow
from bioimageflow.cache import (
    dataframe_result_key,
    processing_prepare_attempt,
    processing_publish,
)
from bioimageflow.storage import CacheCorruptionError, CurrentPointer, RecordManifest, Storage, make_record_id
from bioimageflow_core import Arguments, EnvironmentSpec, ExecutionContext, IOModel, ProcessingTool, Template
from bioimageflow_core.types import ImageSpec, Semantic, SharedArray


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


class FailingDataFrameTool(DataFrameTool):
    display_name = "Failing DataFrame Tool"

    def transform(self, df: pd.DataFrame, arguments) -> pd.DataFrame:
        raise RuntimeError("planned failure")


class MultiRowTable(DataFrameTool):
    display_name = "Multi Row Table"
    accepts_upstream = False
    executions = 0

    def transform(self, df: pd.DataFrame, arguments) -> pd.DataFrame:
        type(self).executions += 1
        return pd.DataFrame(
            {"label": ["alpha", "beta"]},
            index=["a", "b"],
        )


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


class SourceAssetWriter(ProcessingTool):
    display_name = "Source Asset Writer"
    environment = EnvironmentSpec(name="source_asset_writer", dependencies={})
    executions = 0

    class Inputs(IOModel):
        text: str = "mask"

    class Outputs(IOModel):
        mask: Annotated[Path, ImageSpec(semantics={Semantic.LABEL})] = Template("mask_{row_index}.txt")
        count: int

    def process_row(self, arguments: Arguments, *, context: ExecutionContext | None = None):
        type(self).executions += 1
        assert context is not None
        assert "cache/v1/results" in str(context.run_dir)
        assert str(context.run_dir).endswith("/staging")
        mask = Path(arguments.mask)
        assert mask.parent == context.assets_dir
        mask.write_text(arguments.text)
        return self.Outputs(mask=mask, count=len(arguments.text))


class ZeroRowAssetWriter(ProcessingTool):
    display_name = "Zero Row Asset Writer"
    environment = EnvironmentSpec(name="zero_row_asset_writer", dependencies={})
    executions = 0

    class Inputs(IOModel):
        text: str = "blank"

    class Outputs(IOModel):
        mask: Annotated[Path, ImageSpec(semantics={Semantic.LABEL})] = Template("mask_{row_index}.txt")
        count: int

    def process_row(self, arguments: Arguments, *, context: ExecutionContext | None = None):
        type(self).executions += 1
        assert context is not None
        mask = Path(arguments.mask)
        assert mask.parent == context.assets_dir
        mask.write_text(arguments.text)
        return []


class DefaultTemplateZeroRowWriter(ProcessingTool):
    display_name = "Default Template Zero Row Writer"
    environment = EnvironmentSpec(name="default_template_zero_row_writer", dependencies={})
    executions = 0

    class Inputs(IOModel):
        text: str = "default"

    class Outputs(IOModel):
        output: Annotated[Path, ImageSpec(semantics={Semantic.LABEL})]
        count: int

    def process_row(self, arguments: Arguments, *, context: ExecutionContext | None = None):
        type(self).executions += 1
        assert context is not None
        output = Path(arguments.output)
        assert output.parent == context.assets_dir
        output.write_text(arguments.text)
        return []


class SourceExternalPaths(ProcessingTool):
    display_name = "Source External Paths"
    environment = EnvironmentSpec(name="source_external_paths", dependencies={})
    executions = 0

    class Inputs(IOModel):
        directory: Path

    class Outputs(IOModel):
        path: Annotated[Path, ImageSpec(semantics={Semantic.INTENSITY})]
        label: str

    def process_row(self, arguments: Arguments, *, context: object | None = None):
        type(self).executions += 1
        return [
            self.Outputs(path=path, label=path.stem)
            for path in sorted(Path(arguments.directory).glob("*.txt"))
        ]


class FailingSourceAssetWriter(ProcessingTool):
    display_name = "Failing Source Asset Writer"
    environment = EnvironmentSpec(name="failing_source_asset_writer", dependencies={})

    class Inputs(IOModel):
        text: str = "partial"

    class Outputs(IOModel):
        mask: Annotated[Path, ImageSpec(semantics={Semantic.LABEL})] = Template("mask_{row_index}.txt")

    def process_row(self, arguments: Arguments, *, context: object | None = None):
        assert context is not None
        Path(arguments.mask).write_text(arguments.text)
        raise RuntimeError("boom")


class SlowSourceAssetWriter(SourceAssetWriter):
    display_name = "Slow Source Asset Writer"
    environment = EnvironmentSpec(name="slow_source_asset_writer", dependencies={})

    def process_row(self, arguments: Arguments, *, context: object | None = None):
        import time

        time.sleep(0.2)
        return super().process_row(arguments, context=context)


class EscapingSourceAssetWriter(ProcessingTool):
    display_name = "Escaping Source Asset Writer"
    environment = EnvironmentSpec(name="escaping_source_asset_writer", dependencies={})

    class Inputs(IOModel):
        directory: Path

    class Outputs(IOModel):
        mask: Annotated[Path, ImageSpec(semantics={Semantic.LABEL})] = Template("mask_{row_index}.txt")

    def process_row(self, arguments: Arguments, *, context: object | None = None):
        outside = Path(arguments.directory) / "outside.txt"
        outside.write_text("outside")
        return self.Outputs(mask=outside)


class UnsafeTemplateSource(ProcessingTool):
    display_name = "Unsafe Template Source"
    environment = EnvironmentSpec(name="unsafe_template_source", dependencies={})
    executions = 0

    class Inputs(IOModel):
        text: str = "unsafe"

    class Outputs(IOModel):
        mask: Annotated[Path, ImageSpec(semantics={Semantic.LABEL})] = Template("safe.txt")

    def process_row(self, arguments: Arguments, *, context: object | None = None):
        type(self).executions += 1
        Path(arguments.mask).write_text(arguments.text)
        return self.Outputs(mask=arguments.mask)


class ColumnBoundLegacyWriter(ProcessingTool):
    display_name = "Column Bound Legacy Writer"
    environment = EnvironmentSpec(name="column_bound_legacy_writer", dependencies={})
    executions = 0

    class Inputs(IOModel):
        label: str

    class Outputs(IOModel):
        output: Annotated[Path, ImageSpec(semantics={Semantic.LABEL})] = Template("legacy_{row_index}.txt")

    def process_row(self, arguments: Arguments, *, context: object | None = None):
        type(self).executions += 1
        assert context is not None
        output = Path(arguments.output)
        assert "cache/v1/results" in str(context.run_dir)
        assert str(context.run_dir).endswith("/staging")
        assert output.resolve().relative_to(context.assets_dir.resolve())
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(arguments.label)
        return self.Outputs(output=output)


class ColumnBoundZeroRowWriter(ProcessingTool):
    display_name = "Column Bound Zero Row Writer"
    environment = EnvironmentSpec(name="column_bound_zero_row_writer", dependencies={})
    executions = 0

    class Inputs(IOModel):
        label: str

    class Outputs(IOModel):
        output: Annotated[Path, ImageSpec(semantics={Semantic.LABEL})] = Template("zero_{row_index}.txt")
        count: int

    def process_row(self, arguments: Arguments, *, context: object | None = None):
        type(self).executions += 1
        assert context is not None
        output = Path(arguments.output)
        assert output.parent == context.assets_dir
        output.write_text(arguments.label)
        return []


class ColumnBoundZeroRowScalarWriter(ProcessingTool):
    display_name = "Column Bound Zero Row Scalar Writer"
    environment = EnvironmentSpec(name="column_bound_zero_row_scalar_writer", dependencies={})
    executions = 0
    zero_row_scalar_outputs = {"spot_count": 0}

    class Inputs(IOModel):
        label: str

    class Outputs(IOModel):
        spot_id: int
        spot_count: int

    def process_row(self, arguments: Arguments, *, context: object | None = None):
        type(self).executions += 1
        return []


class EscapingColumnBoundWriter(ProcessingTool):
    display_name = "Escaping Column Bound Writer"
    environment = EnvironmentSpec(name="escaping_column_bound_writer", dependencies={})

    class Inputs(IOModel):
        label: str
        directory: Path

    class Outputs(IOModel):
        output: Annotated[Path, ImageSpec(semantics={Semantic.LABEL})] = Template("owned_{row_index}.txt")

    def process_row(self, arguments: Arguments, *, context: object | None = None):
        outside = Path(arguments.directory) / f"{arguments.label}.txt"
        outside.write_text(arguments.label)
        return self.Outputs(output=outside)


class SourceSharedMemoryWriter(ProcessingTool):
    display_name = "Source Shared Memory Writer"
    environment = EnvironmentSpec(name="source_shared_memory_writer", dependencies={})
    executions = 0

    class Inputs(IOModel):
        value: int = 0

    class Outputs(IOModel):
        result: Annotated[SharedArray, ImageSpec(semantics={Semantic.LABEL})]

    def process_row(self, arguments: Arguments, *, context: object | None = None):
        import numpy as np
        from bioimageflow_core.shm import create_shared_output

        type(self).executions += 1
        with create_shared_output(np.full((2, 2), arguments.value, dtype=np.uint8)) as ref:
            return self.Outputs(result=ref)


class SourceFlexibleImageWriter(ProcessingTool):
    display_name = "Source Flexible Image Writer"
    environment = EnvironmentSpec(name="source_flexible_image_writer", dependencies={})
    executions = 0

    class Inputs(IOModel):
        as_shared_array: bool = True

    class Outputs(IOModel):
        result: Annotated[Path | SharedArray, ImageSpec(semantics={Semantic.LABEL})] = Template("flex_{row_index}.txt")

    def process_row(self, arguments: Arguments, *, context: object | None = None):
        type(self).executions += 1
        if arguments.as_shared_array:
            import numpy as np
            from bioimageflow_core.shm import create_shared_output

            with create_shared_output(np.ones((2, 2), dtype=np.uint8)) as ref:
                return self.Outputs(result=ref)
        output = Path(arguments.result)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text("path-result")
        return self.Outputs(result=output)


class SourceSharedMemoryConsumer(ProcessingTool):
    display_name = "Source Shared Memory Consumer"
    environment = EnvironmentSpec(name="source_shared_memory_consumer", dependencies={})

    class Inputs(IOModel):
        result: Annotated[SharedArray, ImageSpec(semantics={Semantic.LABEL})]

    class Outputs(IOModel):
        total: int

    def process_row(self, arguments: Arguments, *, context: object | None = None):
        from bioimageflow_core.io import load_image

        def fail_file_reader(path: Path):
            raise RuntimeError("Should not be called for SharedArray")

        with load_image(arguments.result, file_reader=fail_file_reader) as array:
            return self.Outputs(total=int(array.sum()))


class ColumnBoundSharedMemoryWriter(ProcessingTool):
    display_name = "Column Bound Shared Memory Writer"
    environment = EnvironmentSpec(name="column_bound_shared_memory_writer", dependencies={})
    executions = 0

    class Inputs(IOModel):
        label: str

    class Outputs(IOModel):
        result: Annotated[SharedArray, ImageSpec(semantics={Semantic.LABEL})]

    def process_row(self, arguments: Arguments, *, context: object | None = None):
        import numpy as np
        from bioimageflow_core.shm import create_shared_output

        type(self).executions += 1
        value = len(arguments.label)
        with create_shared_output(np.full((2, 2), value, dtype=np.uint8)) as ref:
            return self.Outputs(result=ref)


class ConstantTableSubWorkflow(SubWorkflow):
    display_name = "Constant Table SubWorkflow"

    class Outputs(IOModel):
        value: int
        label: str

    def build(self, inputs):
        table = CountingTable()(value=16)
        return {"value": table["value"], "label": table["label"]}


def _current_pointer_files(storage_path: Path) -> list[Path]:
    return sorted((storage_path / "cache" / "v1" / "results").glob("*/*/rk_*/current.json"))


def _run_dirs(storage_path: Path) -> list[Path]:
    runs_root = storage_path / "runs"
    if not runs_root.exists():
        return []
    return sorted(path for path in runs_root.iterdir() if path.is_dir())


def _latest_success_run_dir(storage_path: Path) -> Path:
    latest = json.loads((storage_path / "runs" / "latest-success.bioimageflow-link.json").read_text())
    return storage_path / "runs" / latest["target"]


def _invalidated_node_names(invalidated) -> set[str]:
    return {selection.node_name for selection in invalidated}


def _selection_for(invalidated, node_name: str):
    [selection] = [item for item in invalidated if item.node_name == node_name]
    return selection


def _planned_result_key(wf: Workflow, node_name: str) -> str:
    entry = wf.plan()[node_name]
    assert entry.final_result_key is not None
    return entry.final_result_key


def _parquet_digest(df: pd.DataFrame, path: Path) -> str:
    df.to_parquet(path, index=True)
    return f"sha256:{hashlib.sha256(path.read_bytes()).hexdigest()}"


def _write_manual_dataframe_record(storage: Storage, result_key: str, df: pd.DataFrame) -> str:
    scratch = storage.result_dir(result_key) / "manual.parquet"
    scratch.parent.mkdir(parents=True, exist_ok=True)
    dataframe_digest = _parquet_digest(df, scratch)
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
    record_dir = storage.result_dir(result_key) / "records" / record_id
    record_dir.mkdir(parents=True, exist_ok=True)
    scratch.replace(record_dir / "dataframe.parquet")
    manifest = RecordManifest(
        result_key=result_key,
        record_id=record_id,
        dataframe_digest=dataframe_digest,
        outputs=[],
    )
    (record_dir / "manifest.json").write_text(json.dumps(manifest.to_dict(), indent=2, sort_keys=True))
    return record_id


def _force_current_record(storage: Storage, result_key: str, record_id: str) -> None:
    pointer = CurrentPointer(
        result_key=result_key,
        record_id=record_id,
        manifest=f"records/{record_id}/manifest.json",
        attempt_id="manual",
        run_id="manual",
    )
    current_path = storage.result_dir(result_key) / "current.json"
    current_path.write_text(json.dumps(pointer.to_dict(), indent=2, sort_keys=True))


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
    storage = Storage(storage_path)
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


def test_dataframe_tool_progress_events_expose_v1_selection_identity(tmp_path: Path) -> None:
    storage_path = tmp_path / "results"
    CountingTable.executions = 0
    events: list[ProgressEvent] = []

    with Workflow(storage_path=storage_path, on_progress=events.append) as wf:
        node = CountingTable()(value=19)
        wf.compute(node)
        node_name = node.name

    [current_path] = _current_pointer_files(storage_path)
    current = json.loads(current_path.read_text())
    started = [event for event in events if event.node_name == node_name and event.status == "started"]
    completed = [event for event in events if event.node_name == node_name and event.status == "completed"]
    assert started[-1].result_key == current["result_key"]
    assert started[-1].record_id is None
    assert completed[-1].result_key == current["result_key"]
    assert completed[-1].record_id == current["record_id"]

    events.clear()
    with Workflow(storage_path=storage_path, on_progress=events.append) as wf:
        wf.compute(CountingTable()(value=19))

    cached = [event for event in events if event.node_name == node_name and event.status == "cached"]
    assert cached[-1].result_key == current["result_key"]
    assert cached[-1].record_id == current["record_id"]
    assert CountingTable.executions == 1


def test_compute_writes_run_view_for_dataframe_cache_miss_and_hit(tmp_path: Path) -> None:
    storage_path = tmp_path / "results"
    CountingTable.executions = 0

    with Workflow(storage_path=storage_path) as wf:
        node = CountingTable()(value=12)
        wf.compute(node)
        node_name = node.name

    [first_run] = _run_dirs(storage_path)
    first_run_metadata = json.loads((first_run / "run.json").read_text())
    assert first_run_metadata["schema"] == "bioimageflow.run.v1"
    assert first_run_metadata["status"] == "succeeded"
    assert first_run_metadata["target_nodes"] == [node_name]
    first_result = json.loads((first_run / "nodes" / node_name / "result.json").read_text())
    assert first_result["schema"] == "bioimageflow.run.node_result.v1"
    assert first_result["node_key"] == node_name
    assert first_result["cache_hit"] is False
    assert Storage(storage_path).load_current(first_result["result_key"]).record_id == first_result["record_id"]
    assert (first_run / "nodes" / node_name / "record.bioimageflow-link.json").exists()
    assert _latest_success_run_dir(storage_path) == first_run

    with Workflow(storage_path=storage_path) as wf:
        node = CountingTable()(value=12)
        wf.compute(node)

    assert CountingTable.executions == 1
    second_run = [path for path in _run_dirs(storage_path) if path != first_run][0]
    second_result = json.loads((second_run / "nodes" / node_name / "result.json").read_text())
    assert second_result["result_key"] == first_result["result_key"]
    assert second_result["record_id"] == first_result["record_id"]
    assert second_result["cache_hit"] is True
    latest_node = json.loads((storage_path / "latest" / f"{node_name}.bioimageflow-link.json").read_text())
    assert latest_node["target"] == f"../runs/{second_run.name}/nodes/{node_name}"
    assert _latest_success_run_dir(storage_path) == second_run


def test_compute_writes_run_view_output_pointers_for_processing_assets(tmp_path: Path) -> None:
    storage_path = tmp_path / "results"

    with Workflow(storage_path=storage_path) as wf:
        node = SourceAssetWriter()(text="view")
        wf.compute(node)
        node_name = node.name

    [run_dir] = _run_dirs(storage_path)
    result = json.loads((run_dir / "nodes" / node_name / "result.json").read_text())
    assert result["cache_hit"] is False
    assert result["outputs"][0]["kind"] == "owned_asset"
    asset_path = result["outputs"][0]["path"]
    output_link_path = run_dir / "nodes" / node_name / "outputs" / f"{asset_path}.bioimageflow-link.json"
    output_link = json.loads(output_link_path.read_text())
    assert output_link["schema"] == "bioimageflow.link.v1"
    assert output_link["kind"] == "file"
    assert output_link["digest"] == result["outputs"][0]["digest"]
    assert output_link["target"].endswith(f"records/{result['record_id']}/{asset_path}")


def test_compute_steps_writes_run_view_for_cached_step(tmp_path: Path) -> None:
    storage_path = tmp_path / "results"
    CountingTable.executions = 0

    with Workflow(storage_path=storage_path) as wf:
        node = CountingTable()(value=13)
        wf.compute(node)
        node_name = node.name
    assert CountingTable.executions == 1

    events: list[ProgressEvent] = []
    with Workflow(storage_path=storage_path, on_progress=events.append) as wf:
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
    cached = [event for event in events if event.node_name == node_name and event.status == "cached"]
    assert cached[-1].result_key == result["result_key"]
    assert cached[-1].record_id == result["record_id"]


def test_compute_steps_processing_tool_cached_step_emits_v1_progress_identity(tmp_path: Path) -> None:
    storage_path = tmp_path / "results"
    SourceAssetWriter.executions = 0

    with Workflow(storage_path=storage_path) as wf:
        node = SourceAssetWriter()(text="step")
        wf.compute(node)
        node_name = node.name
    assert SourceAssetWriter.executions == 1

    events: list[ProgressEvent] = []
    with Workflow(storage_path=storage_path, on_progress=events.append) as wf:
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
    cached = [event for event in events if event.node_name == node_name and event.status == "cached"]
    assert cached[-1].result_key == result["result_key"]
    assert cached[-1].record_id == result["record_id"]


def test_compute_steps_auto_execute_writes_successful_run_view(tmp_path: Path) -> None:
    storage_path = tmp_path / "results"

    with Workflow(storage_path=storage_path) as wf:
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


def test_failed_compute_keeps_successful_node_latest_without_latest_success(tmp_path: Path) -> None:
    storage_path = tmp_path / "results"

    with Workflow(storage_path=storage_path) as wf:
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
    assert not (storage_path / "runs" / "latest-success.bioimageflow-link.json").exists()
    latest_node = json.loads((storage_path / "latest" / f"{table_name}.bioimageflow-link.json").read_text())
    assert latest_node["target"] == f"../runs/{run_dir.name}/nodes/{table_name}"


def test_failed_parallel_compute_keeps_successful_sibling_run_view(tmp_path: Path) -> None:
    storage_path = tmp_path / "results"

    with Workflow(storage_path=storage_path) as wf:
        slow_success = SlowSourceAssetWriter()(text="ok")
        fast_failure = FailingSourceAssetWriter()(text="boom")
        with pytest.raises(RuntimeError, match="boom"):
            wf.compute(slow_success, fast_failure)
        success_name = slow_success.name

    [run_dir] = _run_dirs(storage_path)
    run = json.loads((run_dir / "run.json").read_text())
    assert run["status"] == "failed"
    assert (run_dir / "nodes" / success_name / "result.json").exists()
    assert not (storage_path / "runs" / "latest-success.bioimageflow-link.json").exists()
    latest_node = json.loads((storage_path / "latest" / f"{success_name}.bioimageflow-link.json").read_text())
    assert latest_node["target"] == f"../runs/{run_dir.name}/nodes/{success_name}"


def test_compute_writes_run_view_for_subworkflow_internal_nodes(tmp_path: Path) -> None:
    storage_path = tmp_path / "results"
    CountingTable.executions = 0

    with Workflow(storage_path=storage_path) as wf:
        node = ConstantTableSubWorkflow()()
        wf.compute(node)
        subworkflow_name = node.name

    [run_dir] = _run_dirs(storage_path)
    internal_result = run_dir / "nodes" / subworkflow_name / "CountingTable_1" / "result.json"
    assert internal_result.exists()
    result = json.loads(internal_result.read_text())
    assert result["node_key"] == f"{subworkflow_name}/CountingTable_1"
    assert result["cache_hit"] is False


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


def test_downstream_result_key_tracks_selected_upstream_record(tmp_path: Path) -> None:
    storage_path = tmp_path / "results"
    CountingTable.executions = 0
    DoubleValue.executions = 0

    with Workflow(storage_path=storage_path) as wf:
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

    with Workflow(storage_path=storage_path) as wf:
        table = CountingTable()(value=4)
        doubled = DoubleValue()(table)
        plan = wf.plan()
        assert plan[table.name].selected_record_id == alternate_record_id
        assert plan[doubled.name].final_result_key != first_downstream_key
        second = wf.compute(doubled)

    assert second.loc["row", "double"] == 198
    assert CountingTable.executions == 1
    assert DoubleValue.executions == 2


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
        result_key = _planned_result_key(wf, node_name)

    storage = Storage(storage_path)
    pointer = storage.load_current(result_key)
    assert pointer is not None
    record_dir = storage.result_dir(result_key) / "records" / pointer.record_id
    assert record_dir.exists()

    with Workflow(storage_path=storage_path) as wf:
        node = CountingTable()(value=9)
        cleared = wf.invalidate([node.name])

    assert _invalidated_node_names(cleared) == {node_name}
    selection = _selection_for(cleared, node_name)
    assert selection.result_key == result_key
    assert selection.selected_record_id == pointer.record_id
    assert selection.status == "removed"
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
        result_key = _planned_result_key(wf, node_name)

    current_path = Storage(storage_path).result_dir(result_key) / "current.json"
    current_path.write_text("{not json")

    with Workflow(storage_path=storage_path) as wf:
        node = CountingTable()(value=10)
        cleared = wf.invalidate([node.name])

    assert _invalidated_node_names(cleared) == {node_name}
    selection = _selection_for(cleared, node_name)
    assert selection.result_key == result_key
    assert selection.selected_record_id is None
    assert selection.status == "corrupt_removed"
    assert not current_path.exists()


def test_dataframe_tool_invalidate_removes_prior_signature_current(tmp_path: Path) -> None:
    storage_path = tmp_path / "results"

    with Workflow(storage_path=storage_path) as wf:
        node = CountingTable()(value=1)
        wf.compute(node)
        old_result_key = _planned_result_key(wf, node.name)

    storage = Storage(storage_path)
    old_pointer = storage.load_current(old_result_key)
    assert old_pointer is not None

    with Workflow(storage_path=storage_path) as wf:
        node = CountingTable()(value=2)
        cleared = wf.invalidate([node.name])

    assert _invalidated_node_names(cleared) == {"CountingTable_1"}
    selection = _selection_for(cleared, "CountingTable_1")
    assert selection.result_key == old_result_key
    assert selection.selected_record_id == old_pointer.record_id
    assert storage.load_current(old_result_key) is None


def test_dataframe_tool_corrupt_v1_dataframe_file_raises_cache_corruption(tmp_path: Path) -> None:
    storage_path = tmp_path / "results"

    with Workflow(storage_path=storage_path) as wf:
        wf.compute(CountingTable()(value=11))
    [current_path] = _current_pointer_files(storage_path)
    current = json.loads(current_path.read_text())
    record_dir = Storage(storage_path).result_dir(current["result_key"]) / "records" / current["record_id"]
    (record_dir / "dataframe.parquet").write_text("not parquet")

    with Workflow(storage_path=storage_path) as wf:
        with pytest.raises(CacheCorruptionError):
            wf.compute(CountingTable()(value=11))


def test_dataframe_tool_publish_rejects_symlinked_record_directory_before_writing(tmp_path: Path) -> None:
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


def test_dataframe_tool_publish_rejects_symlinked_records_directory_before_writing(tmp_path: Path) -> None:
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
        dataframe_publish(storage_path, node_name, sig_hash, pd.DataFrame({"value": [1]}))
    assert list(outside.iterdir()) == []


def test_source_processing_tool_publishes_owned_asset_record_and_uses_cache_hit(tmp_path: Path) -> None:
    storage_path = tmp_path / "results"
    SourceAssetWriter.executions = 0
    events: list[tuple[str, str]] = []

    with Workflow(storage_path=storage_path) as wf:
        node = SourceAssetWriter()(text="abc")
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
            "path": "assets/mask_0.txt",
            "size": 3,
        }
    ]
    assert (record_dir / "assets" / "mask_0.txt").read_text() == "abc"
    assert first.loc["0", "mask"] == str(record_dir / "assets" / "mask_0.txt")
    assert not (storage_path / "data" / "SourceAssetWriter_1").exists()

    with Workflow(storage_path=storage_path, on_progress=lambda event: events.append((event.node_name, event.status))) as wf:
        second = wf.compute(SourceAssetWriter()(text="abc"))

    pd.testing.assert_frame_equal(first, second)
    assert SourceAssetWriter.executions == 1
    assert ("SourceAssetWriter_1", "cached") in events


def test_zero_row_processing_tool_publishes_written_template_asset_without_sentinel_row(
    tmp_path: Path,
) -> None:
    storage_path = tmp_path / "results"
    ZeroRowAssetWriter.executions = 0

    with Workflow(storage_path=storage_path) as wf:
        node = ZeroRowAssetWriter()(text="blank")
        first = wf.compute(node)
        result_key = _planned_result_key(wf, node.name)

    assert first.empty
    assert list(first.columns) == ["mask", "count"]
    storage = Storage(storage_path)
    pointer = storage.load_current(result_key)
    assert pointer is not None
    record_dir = storage.result_dir(result_key) / "records" / pointer.record_id
    manifest = json.loads((record_dir / "manifest.json").read_text())
    assert manifest["outputs"] == [
        {
            "digest": manifest["outputs"][0]["digest"],
            "kind": "owned_asset",
            "output_column": "mask",
            "path": "assets/mask_0.txt",
            "row_index": "0",
            "size": 5,
        }
    ]
    assert (record_dir / "assets" / "mask_0.txt").read_text() == "blank"

    with Workflow(storage_path=storage_path) as wf:
        second = wf.compute(ZeroRowAssetWriter()(text="blank"))

    pd.testing.assert_frame_equal(first, second)
    assert ZeroRowAssetWriter.executions == 1


def test_zero_row_processing_tool_publishes_written_default_template_asset(
    tmp_path: Path,
) -> None:
    storage_path = tmp_path / "results"
    DefaultTemplateZeroRowWriter.executions = 0

    with Workflow(storage_path=storage_path) as wf:
        node = DefaultTemplateZeroRowWriter()(text="default")
        result = wf.compute(node)
        result_key = _planned_result_key(wf, node.name)

    assert result.empty
    storage = Storage(storage_path)
    pointer = storage.load_current(result_key)
    assert pointer is not None
    record_dir = storage.result_dir(result_key) / "records" / pointer.record_id
    manifest = json.loads((record_dir / "manifest.json").read_text())
    assert manifest["outputs"] == [
        {
            "digest": manifest["outputs"][0]["digest"],
            "kind": "owned_asset",
            "output_column": "output",
            "path": "assets/DefaultTemplateZeroRowWriter_1_0",
            "row_index": "0",
            "size": 7,
        }
    ]
    assert (record_dir / "assets" / "DefaultTemplateZeroRowWriter_1_0").read_text() == "default"
    assert DefaultTemplateZeroRowWriter.executions == 1


def test_processing_tool_progress_events_expose_v1_selection_identity(tmp_path: Path) -> None:
    storage_path = tmp_path / "results"
    SourceAssetWriter.executions = 0
    events: list[ProgressEvent] = []

    with Workflow(storage_path=storage_path, on_progress=events.append) as wf:
        node = SourceAssetWriter()(text="progress")
        wf.compute(node)
        node_name = node.name

    [current_path] = _current_pointer_files(storage_path)
    current = json.loads(current_path.read_text())
    started = [event for event in events if event.node_name == node_name and event.status == "started"]
    completed = [event for event in events if event.node_name == node_name and event.status == "completed"]
    assert started[-1].result_key == current["result_key"]
    assert started[-1].record_id is None
    assert completed[-1].result_key == current["result_key"]
    assert completed[-1].record_id == current["record_id"]

    events.clear()
    with Workflow(storage_path=storage_path, on_progress=events.append) as wf:
        wf.compute(SourceAssetWriter()(text="progress"))

    cached = [event for event in events if event.node_name == node_name and event.status == "cached"]
    assert cached[-1].result_key == current["result_key"]
    assert cached[-1].record_id == current["record_id"]
    assert SourceAssetWriter.executions == 1


def test_source_processing_tool_external_paths_stay_external_references(tmp_path: Path) -> None:
    source_dir = tmp_path / "input"
    source_dir.mkdir()
    (source_dir / "a.txt").write_text("a")
    (source_dir / "b.txt").write_text("b")
    storage_path = tmp_path / "results"
    SourceExternalPaths.executions = 0

    with Workflow(storage_path=storage_path) as wf:
        node = SourceExternalPaths()(directory=source_dir)
        first = wf.compute(node)
        result_key = _planned_result_key(wf, node.name)

    storage = Storage(storage_path)
    pointer = storage.load_current(result_key)
    assert pointer is not None
    record_dir = storage.result_dir(result_key) / "records" / pointer.record_id
    manifest = json.loads((record_dir / "manifest.json").read_text())
    assert manifest["outputs"] == [
        {"identity": "path", "kind": "external_path", "path": str(source_dir / "a.txt")},
        {"identity": "path", "kind": "external_path", "path": str(source_dir / "b.txt")},
    ]
    assert set(first["path"]) == {str(source_dir / "a.txt"), str(source_dir / "b.txt")}
    assert not (record_dir / "assets").exists()
    assert not (storage_path / "data" / "SourceExternalPaths_1").exists()

    with Workflow(storage_path=storage_path) as wf:
        second = wf.compute(SourceExternalPaths()(directory=source_dir))

    pd.testing.assert_frame_equal(first, second)
    assert SourceExternalPaths.executions == 1


def test_source_processing_tool_plan_reports_cached_from_v1_current(tmp_path: Path) -> None:
    storage_path = tmp_path / "results"

    with Workflow(storage_path=storage_path) as wf:
        wf.compute(SourceAssetWriter()(text="plan"))

    with Workflow(storage_path=storage_path) as wf:
        node = SourceAssetWriter()(text="plan")
        plan = wf.plan()

    assert plan[node.name].status is NodePlanStatus.CACHED


def test_source_processing_tool_default_input_value_affects_signature(tmp_path: Path) -> None:
    storage_path = tmp_path / "results"

    with Workflow(storage_path=storage_path) as wf:
        explicit = SourceAssetWriter()(text="mask", name="source")
        wf.compute(explicit)
        explicit_sig_hash = wf.plan()[explicit.name].logical_signature

    with Workflow(storage_path=storage_path) as wf:
        defaulted = SourceAssetWriter()(name="source")
        plan = wf.plan()

    assert plan[defaulted.name].logical_signature == explicit_sig_hash
    assert plan[defaulted.name].status is NodePlanStatus.CACHED


def test_source_processing_tool_empty_output_template_override_does_not_claim_external_path(
    tmp_path: Path,
) -> None:
    source_dir = tmp_path / "input"
    source_dir.mkdir()
    (source_dir / "a.txt").write_text("a")
    storage_path = tmp_path / "results"

    with Workflow(storage_path=storage_path) as wf:
        node = SourceExternalPaths()(directory=source_dir, output_templates={"path": ""})
        df = wf.compute(node)
        result_key = _planned_result_key(wf, node.name)

    pointer = Storage(storage_path).load_current(result_key)
    assert pointer is not None
    assert list(df["path"]) == [str(source_dir / "a.txt")]


def test_source_processing_tool_invalidate_removes_current_but_keeps_record(tmp_path: Path) -> None:
    storage_path = tmp_path / "results"
    SourceAssetWriter.executions = 0

    with Workflow(storage_path=storage_path) as wf:
        node = SourceAssetWriter()(text="invalidate")
        wf.compute(node)
        node_name = node.name
        result_key = _planned_result_key(wf, node_name)

    storage = Storage(storage_path)
    pointer = storage.load_current(result_key)
    assert pointer is not None
    record_dir = storage.result_dir(result_key) / "records" / pointer.record_id

    with Workflow(storage_path=storage_path) as wf:
        node = SourceAssetWriter()(text="invalidate")
        cleared = wf.invalidate([node.name])

    assert _invalidated_node_names(cleared) == {node_name}
    selection = _selection_for(cleared, node_name)
    assert selection.result_key == result_key
    assert selection.selected_record_id == pointer.record_id
    assert selection.status == "removed"
    assert storage.load_current(result_key) is None
    assert record_dir.exists()

    with Workflow(storage_path=storage_path) as wf:
        wf.compute(SourceAssetWriter()(text="invalidate"))

    assert SourceAssetWriter.executions == 2


def test_source_processing_tool_invalidate_removes_corrupt_current(tmp_path: Path) -> None:
    storage_path = tmp_path / "results"

    with Workflow(storage_path=storage_path) as wf:
        node = SourceAssetWriter()(text="corrupt")
        wf.compute(node)
        node_name = node.name
        result_key = _planned_result_key(wf, node_name)

    current_path = Storage(storage_path).result_dir(result_key) / "current.json"
    current_path.write_text("{not json")

    with Workflow(storage_path=storage_path) as wf:
        node = SourceAssetWriter()(text="corrupt")
        cleared = wf.invalidate([node.name])

    assert _invalidated_node_names(cleared) == {node_name}
    selection = _selection_for(cleared, node_name)
    assert selection.result_key == result_key
    assert selection.selected_record_id is None
    assert selection.status == "corrupt_removed"
    assert not current_path.exists()


def test_source_processing_tool_invalidate_removes_corrupt_current_with_default_inputs(tmp_path: Path) -> None:
    storage_path = tmp_path / "results"

    with Workflow(storage_path=storage_path) as wf:
        node = SourceAssetWriter()()
        wf.compute(node)
        node_name = node.name
        result_key = _planned_result_key(wf, node_name)

    current_path = Storage(storage_path).result_dir(result_key) / "current.json"
    current_path.write_text("{not json")

    with Workflow(storage_path=storage_path) as wf:
        node = SourceAssetWriter()()
        cleared = wf.invalidate([node.name])

    assert _invalidated_node_names(cleared) == {node_name}
    assert not current_path.exists()


def test_failed_source_processing_tool_does_not_publish_current(tmp_path: Path) -> None:
    storage_path = tmp_path / "results"

    with Workflow(storage_path=storage_path) as wf:
        node = FailingSourceAssetWriter()(text="partial")
        with pytest.raises(RuntimeError, match="boom"):
            wf.compute(node)

    assert _current_pointer_files(storage_path) == []
    assert not (storage_path / "data" / "FailingSourceAssetWriter_1").exists()


def test_source_processing_tool_rejects_templated_output_outside_staging(tmp_path: Path) -> None:
    storage_path = tmp_path / "results"

    with Workflow(storage_path=storage_path) as wf:
        node = EscapingSourceAssetWriter()(directory=tmp_path)
        with pytest.raises(CacheCorruptionError):
            wf.compute(node)

    assert _current_pointer_files(storage_path) == []


@pytest.mark.parametrize("template", ["../outside.txt", "/absolute.txt", r"nested\\backslash.txt"])
def test_source_processing_tool_rejects_unsafe_output_template_before_execution(
    tmp_path: Path,
    template: str,
) -> None:
    storage_path = tmp_path / "results"
    UnsafeTemplateSource.executions = 0

    with Workflow(storage_path=storage_path) as wf:
        node = UnsafeTemplateSource()(output_templates={"mask": template})
        with pytest.raises(CacheCorruptionError):
            wf.compute(node)

    assert UnsafeTemplateSource.executions == 0
    assert _current_pointer_files(storage_path) == []


def test_source_processing_tool_rejects_symlinked_attempts_directory_before_execution(tmp_path: Path) -> None:
    storage_path = tmp_path / "results"
    outside = tmp_path / "outside-attempts"
    outside.mkdir()
    SourceAssetWriter.executions = 0

    with Workflow(storage_path=storage_path) as wf:
        node = SourceAssetWriter()(text="symlink-attempt")
        result_key = _planned_result_key(wf, node.name)
        attempts_dir = Storage(storage_path).result_dir(result_key) / "attempts"
        attempts_dir.parent.mkdir(parents=True)
        attempts_dir.symlink_to(outside)
        with pytest.raises(CacheCorruptionError):
            wf.compute(node)

    assert SourceAssetWriter.executions == 0
    assert list(outside.iterdir()) == []
    assert _current_pointer_files(storage_path) == []


def test_processing_tool_publish_rejects_symlinked_record_assets_before_writing(tmp_path: Path) -> None:
    storage_path = tmp_path / "results"
    node_name = "SourceAssetWriter_1"
    sig_hash = "sig"
    result_key, attempt_id, staging_dir, assets_dir = processing_prepare_attempt(
        storage_path,
        node_name,
        sig_hash,
    )
    source = assets_dir / "mask.txt"
    source.write_text("first")
    df = pd.DataFrame({"mask": [str(source)], "count": [5]}, index=["0"])
    first = processing_publish(
        storage_path,
        node_name,
        sig_hash,
        df,
        result_key=result_key,
        attempt_id=attempt_id,
        staging_dir=staging_dir,
        staging_assets_dir=assets_dir,
        path_columns={"mask"},
        owned_path_columns={"mask"},
    )
    record_dir = Path(first.loc["0", "mask"]).parents[1]
    assets_record_dir = record_dir / "assets"
    import shutil

    shutil.rmtree(assets_record_dir)
    outside = tmp_path / "outside-record-assets"
    outside.mkdir()
    assets_record_dir.symlink_to(outside)

    with pytest.raises(CacheCorruptionError):
        processing_publish(
            storage_path,
            node_name,
            sig_hash,
            df,
            result_key=result_key,
            attempt_id=attempt_id,
            staging_dir=staging_dir,
            staging_assets_dir=assets_dir,
            path_columns={"mask"},
            owned_path_columns={"mask"},
        )
    assert list(outside.iterdir()) == []


def test_processing_tool_publish_accepts_declared_zero_row_owned_asset(tmp_path: Path) -> None:
    storage_path = tmp_path / "results"
    node_name = "ZeroRowAssetWriter_1"
    sig_hash = "sig"
    result_key, attempt_id, staging_dir, assets_dir = processing_prepare_attempt(
        storage_path,
        node_name,
        sig_hash,
    )
    source = assets_dir / "mask_0.txt"
    source.write_text("blank")
    df = pd.DataFrame(columns=pd.Index(["mask", "count"]))

    result = processing_publish(
        storage_path,
        node_name,
        sig_hash,
        df,
        result_key=result_key,
        attempt_id=attempt_id,
        staging_dir=staging_dir,
        staging_assets_dir=assets_dir,
        path_columns={"mask"},
        owned_path_columns={"mask"},
        declared_owned_artifact_paths=[("mask", "0", source)],
    )

    assert result.empty
    pointer = Storage(storage_path).load_current(result_key)
    assert pointer is not None
    record_dir = Storage(storage_path).result_dir(result_key) / "records" / pointer.record_id
    manifest = json.loads((record_dir / "manifest.json").read_text())
    assert manifest["outputs"] == [
        {
            "digest": manifest["outputs"][0]["digest"],
            "kind": "owned_asset",
            "output_column": "mask",
            "path": "assets/mask_0.txt",
            "row_index": "0",
            "size": 5,
        }
    ]
    assert (record_dir / "assets" / "mask_0.txt").read_text() == "blank"


def test_processing_tool_publish_rejects_overlapping_directory_and_child_assets(tmp_path: Path) -> None:
    storage_path = tmp_path / "results"
    node_name = "DirectoryTool_1"
    sig_hash = "sig"
    result_key, attempt_id, staging_dir, assets_dir = processing_prepare_attempt(
        storage_path,
        node_name,
        sig_hash,
    )
    directory = assets_dir / "dataset.zarr"
    directory.mkdir()
    child = directory / "0"
    child.write_text("chunk")
    (directory / "1").write_text("other")
    df = pd.DataFrame(
        {"directory": [str(directory)], "child": [str(child)]},
        index=["0"],
    )

    with pytest.raises(CacheCorruptionError, match="Overlapping owned asset paths"):
        processing_publish(
            storage_path,
            node_name,
            sig_hash,
            df,
            result_key=result_key,
            attempt_id=attempt_id,
            staging_dir=staging_dir,
            staging_assets_dir=assets_dir,
            path_columns={"directory", "child"},
            owned_path_columns={"directory", "child"},
        )


def test_column_bound_processing_tool_publishes_owned_asset_record_and_uses_cache_hit(tmp_path: Path) -> None:
    storage_path = tmp_path / "results"
    ColumnBoundLegacyWriter.executions = 0
    events: list[tuple[str, str]] = []

    with Workflow(storage_path=storage_path) as wf:
        table = CountingTable()(value=4)
        node = ColumnBoundLegacyWriter()(label=table["label"])
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
            "path": "assets/legacy_row.txt",
            "size": 2,
        }
    ]
    assert Path(first.loc["row", "output"]) == record_dir / "assets" / "legacy_row.txt"
    assert Path(first.loc["row", "output"]).read_text() == "v4"
    assert not (storage_path / "data" / "ColumnBoundLegacyWriter_1").exists()

    with Workflow(storage_path=storage_path, on_progress=lambda event: events.append((event.node_name, event.status))) as wf:
        table = CountingTable()(value=4)
        second = wf.compute(ColumnBoundLegacyWriter()(label=table["label"]))

    pd.testing.assert_frame_equal(first, second)
    assert ColumnBoundLegacyWriter.executions == 1
    assert ("ColumnBoundLegacyWriter_1", "cached") in events


def test_column_bound_zero_row_processing_tool_publishes_written_template_assets(
    tmp_path: Path,
) -> None:
    storage_path = tmp_path / "results"
    ColumnBoundZeroRowWriter.executions = 0

    with Workflow(storage_path=storage_path) as wf:
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
    assert [output["output_column"] for output in manifest["outputs"]] == ["output", "output"]
    assert [output["row_index"] for output in manifest["outputs"]] == ["a", "b"]
    assert (record_dir / "assets" / "zero_a.txt").read_text() == "alpha"
    assert (record_dir / "assets" / "zero_b.txt").read_text() == "beta"

    with Workflow(storage_path=storage_path) as wf:
        table = MultiRowTable()()
        second = wf.compute(ColumnBoundZeroRowWriter()(label=table["label"]))

    pd.testing.assert_frame_equal(first, second)
    assert ColumnBoundZeroRowWriter.executions == 2


def test_column_bound_zero_row_processing_tool_publishes_declared_scalar_outputs(
    tmp_path: Path,
) -> None:
    storage_path = tmp_path / "results"
    ColumnBoundZeroRowScalarWriter.executions = 0

    with Workflow(storage_path=storage_path) as wf:
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

    with Workflow(storage_path=storage_path) as wf:
        table = MultiRowTable()()
        second = wf.compute(ColumnBoundZeroRowScalarWriter()(label=table["label"]))

    pd.testing.assert_frame_equal(result, second)
    assert ColumnBoundZeroRowScalarWriter.executions == 2


def test_column_bound_processing_tool_plan_reports_cached_from_v1_current(tmp_path: Path) -> None:
    storage_path = tmp_path / "results"

    with Workflow(storage_path=storage_path) as wf:
        table = CountingTable()(value=5)
        wf.compute(ColumnBoundLegacyWriter()(label=table["label"]))

    with Workflow(storage_path=storage_path) as wf:
        table = CountingTable()(value=5)
        node = ColumnBoundLegacyWriter()(label=table["label"])
        plan = wf.plan()

    assert plan[node.name].status is NodePlanStatus.CACHED


def test_column_bound_processing_tool_invalidate_removes_current_but_keeps_record(tmp_path: Path) -> None:
    storage_path = tmp_path / "results"

    with Workflow(storage_path=storage_path) as wf:
        table = CountingTable()(value=6)
        node = ColumnBoundLegacyWriter()(label=table["label"])
        wf.compute(node)
        node_name = node.name
        result_key = _planned_result_key(wf, node_name)

    storage = Storage(storage_path)
    pointer = storage.load_current(result_key)
    assert pointer is not None
    record_dir = storage.result_dir(result_key) / "records" / pointer.record_id

    with Workflow(storage_path=storage_path) as wf:
        table = CountingTable()(value=6)
        node = ColumnBoundLegacyWriter()(label=table["label"])
        cleared = wf.invalidate([node.name])

    assert _invalidated_node_names(cleared) == {node_name}
    assert storage.load_current(result_key) is None
    assert record_dir.exists()


def test_column_bound_processing_tool_invalidate_removes_prior_signature_current(tmp_path: Path) -> None:
    storage_path = tmp_path / "results"

    with Workflow(storage_path=storage_path) as wf:
        table = CountingTable()(value=9)
        node = ColumnBoundLegacyWriter()(label=table["label"], output_templates={"output": "old_{row_index}.txt"})
        wf.compute(node)
        old_result_key = _planned_result_key(wf, node.name)

    storage = Storage(storage_path)
    assert storage.load_current(old_result_key) is not None

    with Workflow(storage_path=storage_path) as wf:
        table = CountingTable()(value=9)
        node = ColumnBoundLegacyWriter()(label=table["label"], output_templates={"output": "new_{row_index}.txt"})
        cleared = wf.invalidate([node.name])

    assert _invalidated_node_names(cleared) == {"ColumnBoundLegacyWriter_1"}
    selection = _selection_for(cleared, "ColumnBoundLegacyWriter_1")
    assert selection.result_key == old_result_key
    assert selection.status == "removed"
    assert storage.load_current(old_result_key) is None

    with Workflow(storage_path=storage_path) as wf:
        table = CountingTable()(value=9)
        node = ColumnBoundLegacyWriter()(label=table["label"], output_templates={"output": "old_{row_index}.txt"})
        plan = wf.plan()

    assert plan[node.name].status is NodePlanStatus.UNEXECUTED


def test_column_bound_processing_tool_invalidate_keeps_unrelated_metadata_less_current(tmp_path: Path) -> None:
    storage_path = tmp_path / "results"

    with Workflow(storage_path=storage_path) as wf:
        table = CountingTable()(value=10)
        node_a = ColumnBoundLegacyWriter()(label=table["label"], name="writer_a")
        node_b = ColumnBoundLegacyWriter()(label=table["label"], name="writer_b")
        wf.compute(node_a, node_b)
        plan = wf.plan()

    storage = Storage(storage_path)
    key_a = plan["writer_a"].final_result_key
    key_b = plan["writer_b"].final_result_key
    assert key_a is not None
    assert key_b is not None
    (storage.result_dir(key_a) / "result.json").unlink()
    (storage.result_dir(key_b) / "result.json").unlink()

    with Workflow(storage_path=storage_path) as wf:
        table = CountingTable()(value=10)
        node_a = ColumnBoundLegacyWriter()(label=table["label"], name="writer_a")
        ColumnBoundLegacyWriter()(label=table["label"], name="writer_b")
        cleared = wf.invalidate([node_a.name], cascade=False)

    assert _invalidated_node_names(cleared) == {"writer_a"}
    assert storage.load_current(key_a) is None
    assert storage.load_current(key_b) is not None


def test_column_bound_processing_tool_preserves_nested_owned_asset_paths(tmp_path: Path) -> None:
    storage_path = tmp_path / "results"

    with Workflow(storage_path=storage_path) as wf:
        table = MultiRowTable()()
        node = ColumnBoundLegacyWriter()(
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
    assert not (storage_path / "data" / "ColumnBoundLegacyWriter_1").exists()


@pytest.mark.parametrize("template", ["../outside.txt", "/absolute.txt", r"nested\\backslash.txt"])
def test_column_bound_processing_tool_rejects_unsafe_output_template_before_execution(
    tmp_path: Path,
    template: str,
) -> None:
    storage_path = tmp_path / "results"
    ColumnBoundLegacyWriter.executions = 0

    with Workflow(storage_path=storage_path) as wf:
        table = CountingTable()(value=7)
        node = ColumnBoundLegacyWriter()(label=table["label"], output_templates={"output": template})
        with pytest.raises(CacheCorruptionError):
            wf.compute(node)
        result_key = _planned_result_key(wf, node.name)

    assert ColumnBoundLegacyWriter.executions == 0
    assert not (Storage(storage_path).result_dir(result_key) / "current.json").exists()


def test_column_bound_processing_tool_rejects_templated_output_outside_staging(tmp_path: Path) -> None:
    storage_path = tmp_path / "results"

    with Workflow(storage_path=storage_path) as wf:
        table = CountingTable()(value=8)
        node = EscapingColumnBoundWriter()(label=table["label"], directory=tmp_path)
        with pytest.raises(CacheCorruptionError):
            wf.compute(node)
        result_key = _planned_result_key(wf, node.name)

    assert not (Storage(storage_path).result_dir(result_key) / "current.json").exists()


def test_column_bound_shared_array_processing_tool_publishes_durable_v1_asset_and_rehydrates_hits(
    tmp_path: Path,
) -> None:
    from bioimageflow_core.shm import open_shared_array

    storage_path = tmp_path / "results"
    ColumnBoundSharedMemoryWriter.executions = 0

    with Workflow(storage_path=storage_path) as wf:
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
        "digest": output["digest"],
        "kind": "owned_asset",
        "path": output["path"],
        "size": output["size"],
    }
    assert output["path"].startswith("assets/shm/result_")
    assert output["path"].endswith(".npy")
    assert (record_dir / output["path"]).exists()
    assert not (storage_path / "data" / "ColumnBoundSharedMemoryWriter_1").exists()

    events: list[tuple[str, str]] = []
    with Workflow(storage_path=storage_path, on_progress=lambda event: events.append((event.node_name, event.status))) as wf:
        table = CountingTable()(value=4)
        second = wf.compute(ColumnBoundSharedMemoryWriter()(label=table["label"]))

    assert ColumnBoundSharedMemoryWriter.executions == 1
    assert isinstance(second.loc["row", "result"], SharedArray)
    second_ref = second.loc["row", "result"]
    assert second_ref.name != first_ref.name
    with open_shared_array(second_ref) as array:
        assert int(array.sum()) == 8
    assert ("ColumnBoundSharedMemoryWriter_1", "cached") in events

    with Workflow(storage_path=storage_path) as wf:
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

    with Workflow(storage_path=storage_path) as wf:
        table = CountingTable()(value=4)
        node = ColumnBoundSharedMemoryWriter()(label=table["label"])
        assert wf.plan()[node.name].status is NodePlanStatus.CACHED
        cleared = wf.invalidate([node.name])

    assert _invalidated_node_names(cleared) == {"ColumnBoundSharedMemoryWriter_1"}
    assert storage.load_current(result_key) is None
    assert record_dir.exists()


def test_column_bound_shared_array_processing_tool_upstream_change_reports_pending_upstream(tmp_path: Path) -> None:
    storage_path = tmp_path / "results"

    with Workflow(storage_path=storage_path) as wf:
        table = CountingTable()(value=4)
        wf.compute(ColumnBoundSharedMemoryWriter()(label=table["label"]))
        node_name = "ColumnBoundSharedMemoryWriter_1"
        old_result_key = _planned_result_key(wf, node_name)

    assert Storage(storage_path).load_current(old_result_key) is not None
    with Workflow(storage_path=storage_path) as wf:
        table = CountingTable()(value=5)
        node = ColumnBoundSharedMemoryWriter()(label=table["label"])
        plan = wf.plan()

    assert plan[node.name].status is NodePlanStatus.PENDING_UPSTREAM
    assert plan[node.name].pending_upstreams == ("CountingTable_1",)
    assert plan[node.name].final_result_key is None


def test_column_bound_shared_array_plan_and_invalidate_do_not_rehydrate_shared_memory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage_path = tmp_path / "results"

    with Workflow(storage_path=storage_path) as wf:
        table = CountingTable()(value=4)
        wf.compute(ColumnBoundSharedMemoryWriter()(label=table["label"]))

    def fail_create_shared_output(*args, **kwargs):
        raise AssertionError("planning should not create shared-memory segments")

    monkeypatch.setattr("bioimageflow_core.shm.create_shared_output", fail_create_shared_output)

    with Workflow(storage_path=storage_path) as wf:
        table = CountingTable()(value=4)
        node = ColumnBoundSharedMemoryWriter()(label=table["label"])
        plan = wf.plan()

    assert plan[node.name].status is NodePlanStatus.CACHED

    with Workflow(storage_path=storage_path) as wf:
        table = CountingTable()(value=4)
        node = ColumnBoundSharedMemoryWriter()(label=table["label"])
        cleared = wf.invalidate([node.name])

    assert _invalidated_node_names(cleared) == {"ColumnBoundSharedMemoryWriter_1"}


def test_column_bound_shared_array_processing_tool_publishes_one_shared_asset_per_row(tmp_path: Path) -> None:
    from bioimageflow_core.shm import open_shared_array

    storage_path = tmp_path / "results"
    ColumnBoundSharedMemoryWriter.executions = 0

    with Workflow(storage_path=storage_path) as wf:
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
    outputs = sorted(manifest["outputs"], key=lambda output: output["array"]["row_index"])
    assert [output["array"]["row_index"] for output in outputs] == ["a", "b"]
    assert [output["array"]["column"] for output in outputs] == ["result", "result"]
    assert [output["array"]["shape"] for output in outputs] == [[2, 2], [2, 2]]
    assert all(output["asset_role"] == "shared_array" for output in outputs)
    assert all(output["path"].startswith("assets/shm/result_") for output in outputs)
    assert all((record_dir / output["path"]).exists() for output in outputs)

    stored = pd.read_parquet(record_dir / "dataframe.parquet")
    assert all(str(value).startswith("assets/shm/result_") for value in stored["result"])
    assert set(first.index) == {"a", "b"}
    with open_shared_array(first.loc["a", "result"]) as array:
        assert int(array.sum()) == 20
    with open_shared_array(first.loc["b", "result"]) as array:
        assert int(array.sum()) == 16

    with Workflow(storage_path=storage_path) as wf:
        table = MultiRowTable()()
        second = wf.compute(ColumnBoundSharedMemoryWriter()(label=table["label"]))

    assert ColumnBoundSharedMemoryWriter.executions == 2
    with open_shared_array(second.loc["a", "result"]) as array:
        assert int(array.sum()) == 20
    with open_shared_array(second.loc["b", "result"]) as array:
        assert int(array.sum()) == 16


def test_column_bound_shared_array_processing_tool_invalidate_removes_corrupt_current(tmp_path: Path) -> None:
    storage_path = tmp_path / "results"

    with Workflow(storage_path=storage_path) as wf:
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

    with Workflow(storage_path=storage_path) as wf:
        table = CountingTable()(value=4)
        node = ColumnBoundSharedMemoryWriter()(label=table["label"])
        cleared = wf.invalidate([node.name])

    assert _invalidated_node_names(cleared) == {"ColumnBoundSharedMemoryWriter_1"}
    assert not current_path.exists()
    assert record_dir.exists()


def test_column_bound_shared_array_processing_tool_missing_asset_raises_cache_corruption(tmp_path: Path) -> None:
    storage_path = tmp_path / "results"

    with Workflow(storage_path=storage_path) as wf:
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

    with Workflow(storage_path=storage_path) as wf:
        table = CountingTable()(value=4)
        with pytest.raises(CacheCorruptionError, match="Record asset is missing"):
            wf.compute(ColumnBoundSharedMemoryWriter()(label=table["label"]))


def test_source_shared_array_processing_tool_publishes_durable_v1_asset_and_rehydrates_hits(tmp_path: Path) -> None:
    from bioimageflow_core.shm import open_shared_array

    storage_path = tmp_path / "results"
    SourceSharedMemoryWriter.executions = 0

    with Workflow(storage_path=storage_path) as wf:
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
        "digest": output["digest"],
        "kind": "owned_asset",
        "path": output["path"],
        "size": output["size"],
    }
    assert output["path"].startswith("assets/shm/result_")
    assert output["path"].endswith(".npy")
    assert (record_dir / output["path"]).exists()
    assert not (storage_path / "data" / "SourceSharedMemoryWriter_1").exists()

    events: list[tuple[str, str]] = []
    with Workflow(storage_path=storage_path, on_progress=lambda event: events.append((event.node_name, event.status))) as wf:
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

    with Workflow(storage_path=storage_path) as wf:
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

    with Workflow(storage_path=storage_path) as wf:
        node = SourceSharedMemoryWriter()()
        assert wf.plan()[node.name].status is NodePlanStatus.CACHED
        cleared = wf.invalidate([node.name])

    assert _invalidated_node_names(cleared) == {"SourceSharedMemoryWriter_1"}
    assert storage.load_current(result_key) is None
    assert record_dir.exists()


def test_source_shared_array_processing_tool_missing_asset_raises_cache_corruption(tmp_path: Path) -> None:
    storage_path = tmp_path / "results"

    with Workflow(storage_path=storage_path) as wf:
        node = SourceSharedMemoryWriter()()
        wf.compute(node)
        result_key = _planned_result_key(wf, node.name)

    storage = Storage(storage_path)
    pointer = storage.load_current(result_key)
    assert pointer is not None
    record_dir = storage.result_dir(result_key) / "records" / pointer.record_id
    manifest = json.loads((record_dir / "manifest.json").read_text())
    (record_dir / manifest["outputs"][0]["path"]).unlink()

    with Workflow(storage_path=storage_path) as wf:
        with pytest.raises(CacheCorruptionError, match="Record asset is missing"):
            wf.compute(SourceSharedMemoryWriter()())


def test_source_shared_array_processing_tool_parameter_change_reports_prior_selection_miss(tmp_path: Path) -> None:
    storage_path = tmp_path / "results"

    with Workflow(storage_path=storage_path) as wf:
        wf.compute(SourceSharedMemoryWriter()(value=0))

    with Workflow(storage_path=storage_path) as wf:
        node = SourceSharedMemoryWriter()(value=1)
        plan = wf.plan()

    assert plan[node.name].status is NodePlanStatus.PRIOR_SELECTION_MISS


def test_source_path_or_shared_array_output_handles_path_values(tmp_path: Path) -> None:
    storage_path = tmp_path / "results"
    SourceFlexibleImageWriter.executions = 0

    with Workflow(storage_path=storage_path) as wf:
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

    with Workflow(storage_path=storage_path) as wf:
        second = wf.compute(SourceFlexibleImageWriter()(as_shared_array=False))

    assert Path(second.loc["0", "result"]) == result_path
    assert SourceFlexibleImageWriter.executions == 1


def test_source_path_or_shared_array_output_rejects_path_assets_under_shared_namespace(tmp_path: Path) -> None:
    storage_path = tmp_path / "results"

    with Workflow(storage_path=storage_path) as wf:
        node = SourceFlexibleImageWriter()(
            as_shared_array=False,
            output_templates={"result": "shm/path.txt"},
        )
        with pytest.raises(CacheCorruptionError, match="reserved for shared-array assets"):
            wf.compute(node)


def test_source_path_or_shared_array_output_handles_shared_array_values(tmp_path: Path) -> None:
    from bioimageflow_core.shm import open_shared_array

    storage_path = tmp_path / "results"
    SourceFlexibleImageWriter.executions = 0

    with Workflow(storage_path=storage_path) as wf:
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

    with Workflow(storage_path=storage_path) as wf:
        second = wf.compute(SourceFlexibleImageWriter()(as_shared_array=True))

    assert isinstance(second.loc["0", "result"], SharedArray)
    with open_shared_array(second.loc["0", "result"]) as array:
        assert int(array.sum()) == 4
    assert SourceFlexibleImageWriter.executions == 1
