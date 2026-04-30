"""Execution scratch-directory context for ProcessingTool."""

from pathlib import Path
from typing import Any

from bioimageflow import Workflow
from bioimageflow_core import Arguments, ExecutionContext, IOModel, ProcessingTool

from .conftest import FileLoader, imageio_env


class RowContextTool(ProcessingTool):
    display_name = "Row Context Tool"
    environment = imageio_env

    class Inputs(IOModel):
        input_path: Path

    class Outputs(IOModel):
        output_path: Path = "{input_path.stem}.txt"  # type: ignore[assignment]
        work_file: Path
        row_index: str

    def process_row(
        self,
        arguments: Arguments,
        *,
        context: ExecutionContext,
    ) -> Any:
        output_path = Path(arguments.output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text("output")

        work_file = context.work_dir / "implicit.tmp"
        context.work_dir.mkdir(parents=True, exist_ok=True)
        work_file.write_text("scratch")

        return self.Outputs(
            output_path=output_path,
            work_file=work_file,
            row_index=context.row_index,
        )


class BatchContextTool(ProcessingTool):
    display_name = "Batch Context Tool"
    environment = imageio_env

    class Inputs(IOModel):
        input_path: Path

    class Outputs(IOModel):
        output_path: Path = "{input_path.stem}.txt"  # type: ignore[assignment]
        work_dir: Path

    def process_batch(
        self,
        arguments_list: list[Arguments],
        *,
        context: ExecutionContext,
    ) -> Any:
        marker = context.work_dir / "batch.tmp"
        context.work_dir.mkdir(parents=True, exist_ok=True)
        marker.write_text("batch")

        outputs = []
        for arguments in arguments_list:
            output_path = Path(arguments.output_path)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text("output")
            outputs.append(self.Outputs(output_path=output_path, work_dir=context.work_dir))
        return outputs


def test_process_row_receives_per_row_work_dir(tmp_workspace):
    load = FileLoader()
    tool = RowContextTool()

    with Workflow(storage_path=tmp_workspace / "results") as wf:
        raw = load(path=str(tmp_workspace / "data"))
        output = tool(input_path=raw["path"], name="row_context")
        df = wf.compute(output)

    work_files = [Path(str(value)) for value in df["work_file"]]
    assert len(work_files) == 3
    assert len({path.parent for path in work_files}) == 3
    for path in work_files:
        assert path.exists()
        assert path.name == "implicit.tmp"
        assert path.parent.parent.name == "rows"
        assert path.parent.parent.parent.name == "work"
        assert tmp_workspace / "results" in path.parents

    assert not (Path.cwd() / "implicit.tmp").exists()


def test_process_batch_receives_batch_work_dir(tmp_workspace):
    load = FileLoader()
    tool = BatchContextTool()

    with Workflow(storage_path=tmp_workspace / "results") as wf:
        raw = load(path=str(tmp_workspace / "data"))
        output = tool(input_path=raw["path"], name="batch_context")
        df = wf.compute(output)

    work_dirs = {Path(str(value)) for value in df["work_dir"]}
    assert len(work_dirs) == 1
    work_dir = next(iter(work_dirs))
    assert work_dir.name == "batch"
    assert work_dir.parent.name == "work"
    assert (work_dir / "batch.tmp").exists()
