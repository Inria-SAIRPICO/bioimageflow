"""Execution scratch-directory context for ProcessingTool."""

from pathlib import Path
from typing import Any

from bioimageflow import Workflow
from bioimageflow_core import (
    Arguments,
    ExecutionContext,
    IOModel,
    ProcessingTool,
    Template,
)

from .conftest import FileLoader, imageio_env


class RowContextTool(ProcessingTool):
    display_name = "Row Context Tool"
    environment = imageio_env

    class Inputs(IOModel):
        input_path: Path

    class Outputs(IOModel):
        output_path: Path = Template("{input_path.stem}.txt")
        work_file: Path
        work_dir: Path
        rows_dir: Path
        row_dir: Path
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
            work_file=work_file,
            work_dir=context.work_dir,
            rows_dir=context.rows_dir,
            row_dir=context.row_dir,
            row_index=context.row_index,
        )


class BatchContextTool(ProcessingTool):
    display_name = "Batch Context Tool"
    environment = imageio_env

    class Inputs(IOModel):
        input_path: Path

    class Outputs(IOModel):
        output_path: Path = Template("{input_path.stem}.txt")
        work_dir: Path
        batch_dir: Path
        rows_dir: Path

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
                    work_dir=context.work_dir,
                    batch_dir=context.batch_dir,
                    rows_dir=context.rows_dir,
                )
            )
        return outputs


def test_process_row_receives_shared_work_dir_and_per_row_dir(tmp_workspace):
    load = FileLoader()
    tool = RowContextTool()

    with Workflow(storage_path=tmp_workspace / "results") as wf:
        raw = load(path=str(tmp_workspace / "data"))
        output = tool(input_path=raw["path"], name="row_context")
        df = wf.compute(output)

    work_files = [Path(str(value)) for value in df["work_file"]]
    assert len(work_files) == 3
    assert len({path.parent for path in work_files}) == 3

    work_dirs = {Path(str(value)) for value in df["work_dir"]}
    assert len(work_dirs) == 1
    work_dir = next(iter(work_dirs))
    assert work_dir.name == "work"
    assert tmp_workspace / "results" in work_dir.parents

    rows_dirs = {Path(str(value)) for value in df["rows_dir"]}
    assert rows_dirs == {work_dir / "rows"}

    row_dirs = [Path(str(value)) for value in df["row_dir"]]
    assert len(row_dirs) == 3
    assert len(set(row_dirs)) == 3
    for path in work_files:
        assert path.exists()
        assert path.name == "implicit.tmp"
        assert path.parent in row_dirs
        assert path.parent.parent == work_dir / "rows"
        assert tmp_workspace / "results" in path.parents

    assert not (Path.cwd() / "implicit.tmp").exists()


def test_process_batch_receives_shared_work_dir_and_batch_dir(tmp_workspace):
    load = FileLoader()
    tool = BatchContextTool()

    with Workflow(storage_path=tmp_workspace / "results") as wf:
        raw = load(path=str(tmp_workspace / "data"))
        output = tool(input_path=raw["path"], name="batch_context")
        df = wf.compute(output)

    work_dirs = {Path(str(value)) for value in df["work_dir"]}
    assert len(work_dirs) == 1
    work_dir = next(iter(work_dirs))
    assert work_dir.name == "work"

    batch_dirs = {Path(str(value)) for value in df["batch_dir"]}
    assert batch_dirs == {work_dir / "batch"}
    batch_dir = next(iter(batch_dirs))
    assert (batch_dir / "batch.tmp").exists()

    rows_dirs = {Path(str(value)) for value in df["rows_dir"]}
    assert rows_dirs == {work_dir / "rows"}
