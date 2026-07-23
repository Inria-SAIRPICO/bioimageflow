"""Shared helpers for the focused tests split from ``tests/integration/test_runtime_cache.py``."""

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


class CountingTable(DataFrameTool):
    display_name = "Counting Table"
    accepts_upstream = False
    executions = 0

    class Inputs(IOModel):
        value: int = 1

    def transform(self, df: pd.DataFrame, arguments) -> pd.DataFrame:
        type(self).executions += 1
        return pd.DataFrame(
            {"value": [arguments.value], "label": [f"v{arguments.value}"]},
            index=["row"],
        )


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
        mask: Annotated[Path, ImageSpec(semantics={Semantic.LABEL})] = Template(
            "mask_{row_index}.txt"
        )
        count: int

    def process_row(
        self, arguments: Arguments, *, context: ExecutionContext | None = None
    ):
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
        mask: Annotated[Path, ImageSpec(semantics={Semantic.LABEL})] = Template(
            "mask_{row_index}.txt"
        )
        count: int

    def process_row(
        self, arguments: Arguments, *, context: ExecutionContext | None = None
    ):
        type(self).executions += 1
        assert context is not None
        mask = Path(arguments.mask)
        assert mask.parent == context.assets_dir
        mask.write_text(arguments.text)
        return []


class DefaultTemplateZeroRowWriter(ProcessingTool):
    display_name = "Default Template Zero Row Writer"
    environment = EnvironmentSpec(
        name="default_template_zero_row_writer", dependencies={}
    )
    executions = 0

    class Inputs(IOModel):
        text: str = "default"

    class Outputs(IOModel):
        output: Annotated[Path, ImageSpec(semantics={Semantic.LABEL})]
        count: int

    def process_row(
        self, arguments: Arguments, *, context: ExecutionContext | None = None
    ):
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
        mask: Annotated[Path, ImageSpec(semantics={Semantic.LABEL})] = Template(
            "mask_{row_index}.txt"
        )

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
        mask: Annotated[Path, ImageSpec(semantics={Semantic.LABEL})] = Template(
            "mask_{row_index}.txt"
        )

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
        mask: Annotated[Path, ImageSpec(semantics={Semantic.LABEL})] = Template(
            "safe.txt"
        )

    def process_row(self, arguments: Arguments, *, context: object | None = None):
        type(self).executions += 1
        Path(arguments.mask).write_text(arguments.text)
        return self.Outputs(mask=arguments.mask)


class ColumnBoundAssetWriter(ProcessingTool):
    display_name = "Column Bound Asset Writer"
    environment = EnvironmentSpec(name="column_bound_asset_writer", dependencies={})
    executions = 0

    class Inputs(IOModel):
        label: str

    class Outputs(IOModel):
        output: Annotated[Path, ImageSpec(semantics={Semantic.LABEL})] = Template(
            "label_{row_index}.txt"
        )

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
        output: Annotated[Path, ImageSpec(semantics={Semantic.LABEL})] = Template(
            "zero_{row_index}.txt"
        )
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
    environment = EnvironmentSpec(
        name="column_bound_zero_row_scalar_writer", dependencies={}
    )
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
        output: Annotated[Path, ImageSpec(semantics={Semantic.LABEL})] = Template(
            "owned_{row_index}.txt"
        )

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
        with create_shared_output(
            np.full((2, 2), arguments.value, dtype=np.uint8)
        ) as ref:
            return self.Outputs(result=ref)


class SourceFlexibleImageWriter(ProcessingTool):
    display_name = "Source Flexible Image Writer"
    environment = EnvironmentSpec(name="source_flexible_image_writer", dependencies={})
    executions = 0

    class Inputs(IOModel):
        as_shared_array: bool = True

    class Outputs(IOModel):
        result: Annotated[Path | SharedArray, ImageSpec(semantics={Semantic.LABEL})] = (
            Template("flex_{row_index}.txt")
        )

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
    environment = EnvironmentSpec(
        name="column_bound_shared_memory_writer", dependencies={}
    )
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


def _current_pointer_files(storage_path: Path) -> list[Path]:
    return sorted(
        (storage_path / "cache" / "v1" / "results").glob("*/*/rk_*/current.json")
    )


def _run_dirs(storage_path: Path) -> list[Path]:
    runs_root = storage_path / "views" / "runs"
    if not runs_root.exists():
        return []
    return sorted(path for path in runs_root.iterdir() if path.is_dir())


def _latest_success_run_dir(storage_path: Path) -> Path:
    runs_root = storage_path / "views" / "runs"
    latest = json.loads(
        (runs_root / "latest-success.bioimageflow-link.json").read_text()
    )
    return runs_root / latest["target"]


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


def _write_manual_dataframe_record(
    storage: Storage, result_key: str, df: pd.DataFrame
) -> str:
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
    (record_dir / "manifest.json").write_text(
        json.dumps(manifest.to_dict(), indent=2, sort_keys=True)
    )
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
