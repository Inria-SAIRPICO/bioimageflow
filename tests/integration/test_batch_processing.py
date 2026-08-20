"""
Test batch processing via process_batch.

Covers:
- process_batch with 1-to-1 outputs (list[Outputs], auto-wrapped)
- process_batch with 1-to-N outputs (list[list[Outputs]])
- Batch override detection (engine checks type(tool).process_batch)
"""


from pathlib import Path
from typing import Any

import pandas as pd

from bioimageflow import DataFrameTool
from bioimageflow import Workflow
from bioimageflow_core import Arguments, IOModel, ProcessingTool, RowConsumption, Template

from tests.testkit.integration_tools import (
    FileLoader,
    StubBatchExploder,
    StubBatchProcessor,
)


class EmptySource(DataFrameTool):
    class Inputs(IOModel):
        pass

    class Outputs(IOModel):
        value: int

    def transform(self, df: Any, arguments: Arguments) -> pd.DataFrame:
        return pd.DataFrame(columns=pd.Index(["value"]))


class EmptyChild(DataFrameTool):
    class Inputs(IOModel):
        pass

    class Outputs(IOModel):
        value: int

    def transform(self, df: Any, arguments: Arguments) -> pd.DataFrame:
        return pd.DataFrame(columns=pd.Index(["value"]))


class SinglePathSource(DataFrameTool):
    class Inputs(IOModel):
        path: Path

    class Outputs(IOModel):
        path: Path

    def transform(self, df: Any, arguments: Arguments) -> pd.DataFrame:
        return pd.DataFrame([{"path": str(arguments.path)}], index=pd.Index(["0"]))


class EmptyBatchProbe(ProcessingTool):
    row_consumption = RowConsumption.MAPPED
    environment = StubBatchProcessor.environment
    called = False

    class Inputs(IOModel):
        value: int

    class Outputs(IOModel):
        output: Path = Template("probe.txt")

    def process_batch(self, arguments_list: list[Any], *, context: object | None = None) -> Any:
        type(self).called = True
        return []


class EmptyBatchReducer(ProcessingTool):
    row_consumption = RowConsumption.MAPPED
    environment = StubBatchProcessor.environment
    run_empty_batch = True

    class Inputs(IOModel):
        value: int

    class Outputs(IOModel):
        output: Path = Template("empty_reducer.txt")
        count: int

    def process_batch(self, arguments_list: list[Any], *, context: object | None = None) -> Any:
        assert len(arguments_list) == 1
        output = Path(arguments_list[0].output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text("empty")
        return [[self.Outputs(output=output, count=0)]]


class AnchoredEmptyBatchReducer(ProcessingTool):
    row_consumption = RowConsumption.MAPPED
    environment = StubBatchProcessor.environment
    run_empty_batch = True
    empty_batch_anchor_inputs = ("path",)

    class Inputs(IOModel):
        value: int
        path: Path

    class Outputs(IOModel):
        output: Path = Template("{path.stem}_empty.txt")
        source_name: str

    def process_batch(self, arguments_list: list[Any], *, context: object | None = None) -> Any:
        rows = []
        for arguments in arguments_list:
            output = Path(arguments.output)
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(Path(arguments.path).name)
            rows.append([self.Outputs(output=output, source_name=Path(arguments.path).name)])
        return rows


class TestBatchOneToOne:

    def test_batch_processor_runs_all_rows(self, tmp_workspace):
        load = FileLoader()
        batch = StubBatchProcessor()

        with Workflow(engine="direct", storage_path=tmp_workspace / "results") as wf:
            raw = load(path=str(tmp_workspace / "data"))
            embeddings = batch(input_image=raw["path"])
            df = wf.compute(embeddings)
            assert len(df) == 3
            assert "embedding" in df.columns

    def test_batch_output_files_exist(self, tmp_workspace):
        load = FileLoader()
        batch = StubBatchProcessor()

        with Workflow(engine="direct", storage_path=tmp_workspace / "results") as wf:
            raw = load(path=str(tmp_workspace / "data"))
            embeddings = batch(input_image=raw["path"])
            df = wf.compute(embeddings)
            from pathlib import Path

            for _, row in df.iterrows():
                assert Path(row["embedding"]).exists()


class TestBatchOneToN:

    def test_batch_exploder_produces_multiple_outputs(self, tmp_workspace):
        load = FileLoader()
        exploder = StubBatchExploder()

        with Workflow(engine="direct", storage_path=tmp_workspace / "results") as wf:
            raw = load(path=str(tmp_workspace / "data"))
            crops = exploder(input_image=raw["path"])
            df = wf.compute(crops)
            # 3 images × 2 crops each = 6 rows
            assert len(df) == 6
            assert "crop" in df.columns
            # Index should be exploded
            for idx in df.index:
                assert "::" in str(idx)


class TestBatchOverrideDetection:

    def test_batch_tool_is_detected_as_batch(self):
        """Engine detects process_batch override."""
        assert type(StubBatchProcessor()).process_batch is not ProcessingTool.process_batch

    def test_row_tool_is_not_batch(self):
        from tests.testkit.integration_tools import StubSegmenter
        assert type(StubSegmenter()).process_batch is ProcessingTool.process_batch


class TestEmptyBatchExecution:

    def test_default_batch_tool_does_not_run_for_empty_upstream(self, tmp_workspace):
        EmptyBatchProbe.called = False

        with Workflow(engine="direct", storage_path=tmp_workspace / "results") as wf:
            empty = EmptySource()(name="empty")
            probed = EmptyBatchProbe()(value=empty["value"], name="probe")
            df = wf.compute(probed)

        assert not EmptyBatchProbe.called
        assert df.empty
        assert list(df.columns) == ["output"]

    def test_run_empty_batch_tool_runs_once_for_empty_upstream(self, tmp_workspace):
        with Workflow(engine="direct", storage_path=tmp_workspace / "results") as wf:
            empty = EmptySource()(name="empty")
            reduced = EmptyBatchReducer()(value=empty["value"], name="reduce_empty")
            df = wf.compute(reduced)

        assert len(df) == 1
        assert int(df.iloc[0]["count"]) == 0
        assert Path(df.iloc[0]["output"]).read_text() == "empty"

    def test_run_empty_batch_anchor_uses_non_empty_bound_input(self, tmp_workspace):
        source_path = tmp_workspace / "source.tif"
        source_path.write_text("source")

        with Workflow(engine="direct", storage_path=tmp_workspace / "results") as wf:
            source = SinglePathSource()(path=source_path, name="source")
            empty = EmptyChild()(source, name="empty")
            reduced = AnchoredEmptyBatchReducer()(
                value=empty["value"],
                path=source["path"],
                name="anchored",
            )
            df = wf.compute(reduced)

        assert len(df) == 1
        assert df.iloc[0]["source_name"] == "source.tif"
        assert Path(df.iloc[0]["output"]).read_text() == "source.tif"
