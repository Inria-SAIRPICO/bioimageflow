"""Execution scratch-directory context for ProcessingTool."""

from pathlib import Path
from typing import Any

from bioimageflow import Workflow
from bioimageflow_core import (
    Arguments,
    ExecutionContext,
    IOModel,
    ProcessingTool,
    RowConsumption,
    Template,
)

from tests.testkit.integration_tools import FileLoader, imageio_env


class RowContextTool(ProcessingTool):
    row_consumption = RowConsumption.MAPPED
    display_name = "Row Context Tool"
    environment = imageio_env

    class Inputs(IOModel):
        input_path: Path

    class Outputs(IOModel):
        output_path: Path = Template("{input_path.stem}.txt")
        work_file_name: str
        work_dir_name: str
        rows_dir_name: str
        row_dir_name: str
        row_index: str

    def process_row(
        self,
        arguments: Arguments,
        *,
        context: ExecutionContext | None = None,
    ) -> Any:
        assert context is not None
        output_path = Path(arguments.output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text("output")

        assert context.row_dir is not None
        assert context.rows_dir is not None
        assert context.row_dir.parent == context.rows_dir
        assert context.rows_dir.parent == context.work_dir

        work_file = context.row_dir / "implicit.tmp"
        context.row_dir.mkdir(parents=True, exist_ok=True)
        work_file.write_text("scratch")

        return self.Outputs(
            output_path=output_path,
            work_file_name=work_file.name,
            work_dir_name=context.work_dir.name,
            rows_dir_name=context.rows_dir.name,
            row_dir_name=context.row_dir.name,
            row_index=context.row_index,
        )


class BatchContextTool(ProcessingTool):
    row_consumption = RowConsumption.MAPPED
    display_name = "Batch Context Tool"
    environment = imageio_env

    class Inputs(IOModel):
        input_path: Path

    class Outputs(IOModel):
        output_path: Path = Template("{input_path.stem}.txt")
        work_dir_name: str
        batch_dir_name: str
        rows_dir_name: str

    def process_batch(
        self,
        arguments_list: list[Arguments],
        *,
        context: ExecutionContext | None = None,
    ) -> Any:
        assert context is not None
        assert context.batch_dir is not None
        assert context.rows_dir is not None
        assert context.batch_dir.parent == context.work_dir
        assert context.rows_dir.parent == context.work_dir

        marker = context.batch_dir / "batch.tmp"
        context.batch_dir.mkdir(parents=True, exist_ok=True)
        marker.write_text("batch")

        outputs = []
        for arguments in arguments_list:
            output_path = Path(arguments.output_path)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text("output")
            outputs.append(
                self.Outputs(
                    output_path=output_path,
                    work_dir_name=context.work_dir.name,
                    batch_dir_name=context.batch_dir.name,
                    rows_dir_name=context.rows_dir.name,
                )
            )
        return outputs


def test_process_row_receives_shared_work_dir_and_per_row_dir(tmp_workspace):
    load = FileLoader()
    tool = RowContextTool()

    with Workflow(engine="direct", storage_path=tmp_workspace / "results") as wf:
        raw = load(path=str(tmp_workspace / "data"))
        output = tool(input_path=raw["path"], name="row_context")
        df = wf.compute(output)

    assert set(df["work_file_name"]) == {"implicit.tmp"}
    assert set(df["work_dir_name"]) == {"work"}
    assert set(df["rows_dir_name"]) == {"rows"}
    assert len(set(df["row_dir_name"])) == 3
    work_files = list(
        (tmp_workspace / "results" / "cache" / "v1" / "results").glob(
            "**/staging/work/rows/*/implicit.tmp"
        )
    )
    assert len(work_files) == 3
    for path in work_files:
        assert path.exists()
        assert path.name == "implicit.tmp"
        assert path.parent.parent.name == "rows"
        assert path.parent.parent.parent.name == "work"
        assert tmp_workspace / "results" in path.parents

    assert not (Path.cwd() / "implicit.tmp").exists()


def test_process_batch_receives_shared_work_dir_and_batch_dir(tmp_workspace):
    load = FileLoader()
    tool = BatchContextTool()

    with Workflow(engine="direct", storage_path=tmp_workspace / "results") as wf:
        raw = load(path=str(tmp_workspace / "data"))
        output = tool(input_path=raw["path"], name="batch_context")
        df = wf.compute(output)

    assert set(df["work_dir_name"]) == {"work"}
    assert set(df["batch_dir_name"]) == {"batch"}
    assert set(df["rows_dir_name"]) == {"rows"}
    markers = list(
        (tmp_workspace / "results" / "cache" / "v1" / "results").glob(
            "**/staging/work/batch/batch.tmp"
        )
    )
    assert len(markers) == 1
    assert markers[0].exists()
