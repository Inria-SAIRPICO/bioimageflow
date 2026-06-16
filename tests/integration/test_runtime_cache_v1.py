"""Runtime integration tests for the v1 output/cache storage contract."""

import json
import hashlib
from pathlib import Path
from typing import Annotated

import pandas as pd
import pytest

from bioimageflow import NodePlanStatus, Workflow
from bioimageflow.dataframe_tool import DataFrameTool
from bioimageflow.cache import (
    dataframe_v1_result_key,
    processing_v1_prepare_attempt,
    processing_v1_publish,
    processing_v1_result_key,
)
from bioimageflow.storage_v1 import CacheCorruptionError, StorageV1, make_record_id
from bioimageflow_core import Arguments, EnvironmentSpec, IOModel, ProcessingTool, Template
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

    def process_row(self, arguments: Arguments, *, context: object | None = None):
        type(self).executions += 1
        assert context is not None
        assert "cache/v1/results" in str(context.run_dir)
        assert str(context.run_dir).endswith("/staging")
        mask = Path(arguments.mask)
        assert mask.parent == context.assets_dir
        mask.write_text(arguments.text)
        return self.Outputs(mask=mask, count=len(arguments.text))


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

    class Outputs(IOModel):
        result: Annotated[SharedArray, ImageSpec(semantics={Semantic.LABEL})]

    def process_row(self, arguments: Arguments, *, context: object | None = None):
        import numpy as np
        from bioimageflow_core.shm import create_shared_output

        with create_shared_output(np.zeros((2, 2), dtype=np.uint8)) as ref:
            return self.Outputs(result=ref)


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


def test_source_processing_tool_publishes_owned_asset_record_and_uses_cache_hit(tmp_path: Path) -> None:
    storage_path = tmp_path / "results"
    SourceAssetWriter.executions = 0
    events: list[tuple[str, str]] = []

    with Workflow(storage_path=storage_path) as wf:
        node = SourceAssetWriter()(text="abc")
        first = wf.compute(node)
        sig_hash = wf.plan()[node.name].sig_hash

    result_key = processing_v1_result_key("SourceAssetWriter_1", sig_hash)
    storage = StorageV1(storage_path)
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
        sig_hash = wf.plan()[node.name].sig_hash

    result_key = processing_v1_result_key("SourceExternalPaths_1", sig_hash)
    storage = StorageV1(storage_path)
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
        explicit_sig_hash = wf.plan()[explicit.name].sig_hash

    with Workflow(storage_path=storage_path) as wf:
        defaulted = SourceAssetWriter()(name="source")
        plan = wf.plan()

    assert plan[defaulted.name].sig_hash == explicit_sig_hash
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
        sig_hash = wf.plan()[node.name].sig_hash

    result_key = processing_v1_result_key("SourceExternalPaths_1", sig_hash)
    pointer = StorageV1(storage_path).load_current(result_key)
    assert pointer is not None
    assert list(df["path"]) == [str(source_dir / "a.txt")]


def test_source_processing_tool_invalidate_removes_current_but_keeps_record(tmp_path: Path) -> None:
    storage_path = tmp_path / "results"
    SourceAssetWriter.executions = 0

    with Workflow(storage_path=storage_path) as wf:
        node = SourceAssetWriter()(text="invalidate")
        wf.compute(node)
        node_name = node.name
        sig_hash = wf.plan()[node_name].sig_hash

    result_key = processing_v1_result_key(node_name, sig_hash)
    storage = StorageV1(storage_path)
    pointer = storage.load_current(result_key)
    assert pointer is not None
    record_dir = storage.result_dir(result_key) / "records" / pointer.record_id

    with Workflow(storage_path=storage_path) as wf:
        node = SourceAssetWriter()(text="invalidate")
        cleared = wf.invalidate([node.name])

    assert cleared == {node_name}
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
        sig_hash = wf.plan()[node_name].sig_hash

    result_key = processing_v1_result_key(node_name, sig_hash)
    current_path = StorageV1(storage_path).result_dir(result_key) / "current.json"
    current_path.write_text("{not json")

    with Workflow(storage_path=storage_path) as wf:
        node = SourceAssetWriter()(text="corrupt")
        cleared = wf.invalidate([node.name])

    assert cleared == {node_name}
    assert not current_path.exists()


def test_source_processing_tool_invalidate_removes_corrupt_current_with_default_inputs(tmp_path: Path) -> None:
    storage_path = tmp_path / "results"

    with Workflow(storage_path=storage_path) as wf:
        node = SourceAssetWriter()()
        wf.compute(node)
        node_name = node.name
        sig_hash = wf.plan()[node_name].sig_hash

    result_key = processing_v1_result_key(node_name, sig_hash)
    current_path = StorageV1(storage_path).result_dir(result_key) / "current.json"
    current_path.write_text("{not json")

    with Workflow(storage_path=storage_path) as wf:
        node = SourceAssetWriter()()
        cleared = wf.invalidate([node.name])

    assert cleared == {node_name}
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
        sig_hash = wf.plan()[node.name].sig_hash
        result_key = processing_v1_result_key(node.name, sig_hash)
        attempts_dir = StorageV1(storage_path).result_dir(result_key) / "attempts"
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
    result_key, attempt_id, staging_dir, assets_dir = processing_v1_prepare_attempt(
        storage_path,
        node_name,
        sig_hash,
    )
    source = assets_dir / "mask.txt"
    source.write_text("first")
    df = pd.DataFrame({"mask": [str(source)], "count": [5]}, index=["0"])
    first = processing_v1_publish(
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
        processing_v1_publish(
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


def test_processing_tool_publish_rejects_overlapping_directory_and_child_assets(tmp_path: Path) -> None:
    storage_path = tmp_path / "results"
    node_name = "DirectoryTool_1"
    sig_hash = "sig"
    result_key, attempt_id, staging_dir, assets_dir = processing_v1_prepare_attempt(
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
        processing_v1_publish(
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
        sig_hash = wf.plan()[node.name].sig_hash

    result_key = processing_v1_result_key("ColumnBoundLegacyWriter_1", sig_hash)
    storage = StorageV1(storage_path)
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
        sig_hash = wf.plan()[node_name].sig_hash

    result_key = processing_v1_result_key(node_name, sig_hash)
    storage = StorageV1(storage_path)
    pointer = storage.load_current(result_key)
    assert pointer is not None
    record_dir = storage.result_dir(result_key) / "records" / pointer.record_id

    with Workflow(storage_path=storage_path) as wf:
        table = CountingTable()(value=6)
        node = ColumnBoundLegacyWriter()(label=table["label"])
        cleared = wf.invalidate([node.name])

    assert cleared == {node_name}
    assert storage.load_current(result_key) is None
    assert record_dir.exists()


def test_column_bound_processing_tool_invalidate_removes_prior_signature_current(tmp_path: Path) -> None:
    storage_path = tmp_path / "results"

    with Workflow(storage_path=storage_path) as wf:
        table = CountingTable()(value=9)
        node = ColumnBoundLegacyWriter()(label=table["label"], output_templates={"output": "old_{row_index}.txt"})
        wf.compute(node)
        old_sig_hash = wf.plan()[node.name].sig_hash

    old_result_key = processing_v1_result_key("ColumnBoundLegacyWriter_1", old_sig_hash)
    storage = StorageV1(storage_path)
    assert storage.load_current(old_result_key) is not None

    with Workflow(storage_path=storage_path) as wf:
        table = CountingTable()(value=9)
        node = ColumnBoundLegacyWriter()(label=table["label"], output_templates={"output": "new_{row_index}.txt"})
        cleared = wf.invalidate([node.name])

    assert cleared == {"ColumnBoundLegacyWriter_1"}
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

    storage = StorageV1(storage_path)
    key_a = processing_v1_result_key("writer_a", plan["writer_a"].sig_hash)
    key_b = processing_v1_result_key("writer_b", plan["writer_b"].sig_hash)
    (storage.result_dir(key_a) / "result.json").unlink()
    (storage.result_dir(key_b) / "result.json").unlink()

    with Workflow(storage_path=storage_path) as wf:
        table = CountingTable()(value=10)
        node_a = ColumnBoundLegacyWriter()(label=table["label"], name="writer_a")
        ColumnBoundLegacyWriter()(label=table["label"], name="writer_b")
        cleared = wf.invalidate([node_a.name], cascade=False)

    assert cleared == {"writer_a"}
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
        sig_hash = wf.plan()[node.name].sig_hash

    result_key = processing_v1_result_key("ColumnBoundLegacyWriter_1", sig_hash)
    storage = StorageV1(storage_path)
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
        sig_hash = wf.plan()[node.name].sig_hash
        with pytest.raises(CacheCorruptionError):
            wf.compute(node)

    assert ColumnBoundLegacyWriter.executions == 0
    assert not (StorageV1(storage_path).result_dir(processing_v1_result_key(node.name, sig_hash)) / "current.json").exists()


def test_column_bound_processing_tool_rejects_templated_output_outside_staging(tmp_path: Path) -> None:
    storage_path = tmp_path / "results"

    with Workflow(storage_path=storage_path) as wf:
        table = CountingTable()(value=8)
        node = EscapingColumnBoundWriter()(label=table["label"], directory=tmp_path)
        sig_hash = wf.plan()[node.name].sig_hash
        with pytest.raises(CacheCorruptionError):
            wf.compute(node)

    assert not (StorageV1(storage_path).result_dir(processing_v1_result_key(node.name, sig_hash)) / "current.json").exists()


def test_column_bound_shared_array_processing_tool_stays_legacy_until_durable_v1(tmp_path: Path) -> None:
    from .conftest import FileLoader, StubSharedMemoryTool

    storage_path = tmp_path / "results"
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "image.tif").write_text("image")

    with Workflow(storage_path=storage_path) as wf:
        raw = FileLoader()(path=str(data_dir))
        node = StubSharedMemoryTool()(input_image=raw["path"])
        wf.compute(node)

    assert (storage_path / "data" / "StubSharedMemoryTool_1").exists()


def test_source_shared_array_processing_tool_stays_legacy_until_durable_v1(tmp_path: Path) -> None:
    storage_path = tmp_path / "results"

    with Workflow(storage_path=storage_path) as wf:
        node = SourceSharedMemoryWriter()()
        wf.compute(node)

    assert (storage_path / "data" / "SourceSharedMemoryWriter_1").exists()
    assert _current_pointer_files(storage_path) == []

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

    with Workflow(storage_path=storage_path) as wf:
        node = SourceSharedMemoryWriter()()
        assert wf.plan()[node.name].status is NodePlanStatus.CACHED
        cleared = wf.invalidate([node.name])

    assert cleared == {"SourceSharedMemoryWriter_1"}
    assert not (storage_path / "data" / "SourceSharedMemoryWriter_1").exists()
