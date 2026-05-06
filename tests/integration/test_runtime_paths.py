"""Runtime path normalization contracts."""

import subprocess
import sys
from pathlib import Path
from typing import Annotated, Any

import pandas as pd

from bioimageflow import DataFrameTool, Workflow
from bioimageflow_core import Arguments, IOModel, ProcessingTool, Template
from bioimageflow_core.environment import EnvironmentSpec
from bioimageflow_core.types import ImageSpec, Semantic


class RelativePathSource(DataFrameTool):
    """Return a relative path even though the output schema is path-typed."""

    class Inputs(IOModel):
        directory: Path

    class Outputs(IOModel):
        input_image: Annotated[Path, ImageSpec(semantics={Semantic.INTENSITY})]

    def transform(self, df: Any, arguments: Any) -> pd.DataFrame:
        relative_path = Path(arguments.directory).relative_to(Path.cwd()) / "input.txt"
        return pd.DataFrame([{"input_image": relative_path}])


class CwdSensitiveWrapper(ProcessingTool):
    """Wrapper that passes framework paths to a subprocess with cwd=work_dir."""

    environment = EnvironmentSpec(name="runtime-paths", dependencies={})

    class Inputs(IOModel):
        input_image: Annotated[Path, ImageSpec(semantics={Semantic.INTENSITY})]

    class Outputs(IOModel):
        output_image: Annotated[Path, ImageSpec(semantics={Semantic.LABEL})] = Template(
            "{input_image.stem}_checked.txt"
        )

    def process_row(self, arguments: Arguments, *, context: Any = None) -> Any:
        assert context is not None
        input_path = Path(arguments.input_image)
        output_path = Path(arguments.output_image)
        assert input_path.is_absolute()
        assert output_path.is_absolute()
        assert context.work_dir.is_absolute()

        subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "from pathlib import Path; import sys; "
                    "Path(sys.argv[2]).write_text(Path(sys.argv[1]).read_text())"
                ),
                str(input_path),
                str(output_path),
            ],
            check=True,
            cwd=context.work_dir,
        )
        return self.Outputs(output_image=output_path)


def test_runtime_paths_are_absolute_when_subprocess_uses_work_dir(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    monkeypatch.chdir(tmp_path)
    data_dir = Path("data")
    data_dir.mkdir()
    (data_dir / "input.txt").write_text("ok")

    source = RelativePathSource()
    wrapper = CwdSensitiveWrapper()

    with Workflow(
        storage_path=Path("relative_results"),
        use_wetlands=False,
    ) as workflow:
        raw = source(directory=data_dir)
        checked = wrapper(input_image=raw["input_image"])
        df = workflow.compute(checked)

    output_path = Path(df.at["0", "output_image"])
    assert workflow.storage_path.is_absolute()
    assert output_path.is_absolute()
    assert output_path.read_text() == "ok"
