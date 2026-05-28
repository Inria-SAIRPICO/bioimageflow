"""Worker-side forwarding of ExecutionContext."""

import json
import sys

import pytest
from bioimageflow_core import ExecutionContext
from bioimageflow_core.worker import run_process_batch, run_process_row


def test_worker_forwards_row_execution_context(tmp_path):
    tool_file = tmp_path / "ctx_tool.py"
    tool_file.write_text(
        """
from pathlib import Path
from typing import Any
from bioimageflow_core import Arguments, EnvironmentSpec, ExecutionContext, IOModel, ProcessingTool

env = EnvironmentSpec(name="ctx", dependencies={})

class ContextTool(ProcessingTool):
    environment = env

    class Inputs(IOModel):
        value: str

    class Outputs(IOModel):
        seen: str

    def process_row(self, arguments: Arguments, *, context: ExecutionContext) -> Any:
        assert context.run_dir == Path(arguments.run_dir)
        assert context.assets_dir == Path(arguments.run_dir) / "assets"
        assert context.work_dir == Path(arguments.run_dir) / "work"
        assert context.rows_dir == Path(arguments.run_dir) / "work" / "rows"
        assert context.row_dir == Path(arguments.run_dir) / "work" / "rows" / "000000"
        assert context.batch_dir is None
        assert context.row_index == "sample"
        return self.Outputs(seen=str(context.row_dir / arguments.value))
"""
    )
    run_dir = tmp_path / "run"

    result = run_process_row(
        (
            str(tool_file),
            "ContextTool",
            {"value": "marker", "run_dir": str(run_dir)},
            {
                "run_dir": str(run_dir),
                "assets_dir": str(run_dir / "assets"),
                "work_dir": str(run_dir / "work"),
                "rows_dir": str(run_dir / "work" / "rows"),
                "row_dir": str(run_dir / "work" / "rows" / "000000"),
                "batch_dir": None,
                "row_index": "sample",
            },
        )
    )

    assert result == [{"seen": str(run_dir / "work" / "rows" / "000000" / "marker")}]


def test_worker_forwards_batch_execution_context(tmp_path):
    tool_file = tmp_path / "batch_ctx_tool.py"
    tool_file.write_text(
        """
from pathlib import Path
from typing import Any
from bioimageflow_core import Arguments, EnvironmentSpec, ExecutionContext, IOModel, ProcessingTool

env = EnvironmentSpec(name="ctx", dependencies={})

class BatchContextTool(ProcessingTool):
    environment = env

    class Inputs(IOModel):
        value: str

    class Outputs(IOModel):
        seen: str

    def process_batch(self, arguments_list: list[Arguments], *, context: ExecutionContext) -> Any:
        run_dir = Path(arguments_list[0].run_dir)
        assert context.run_dir == run_dir
        assert context.assets_dir == run_dir / "assets"
        assert context.work_dir == run_dir / "work"
        assert context.rows_dir == run_dir / "work" / "rows"
        assert context.row_dir is None
        assert context.batch_dir == run_dir / "work" / "batch"
        assert context.row_index is None
        return [self.Outputs(seen=str(context.batch_dir / args.value)) for args in arguments_list]
"""
    )
    run_dir = tmp_path / "run"

    result = run_process_batch(
        str(tool_file),
        "BatchContextTool",
        [{"value": "a", "run_dir": str(run_dir)}, {"value": "b", "run_dir": str(run_dir)}],
        {
            "run_dir": str(run_dir),
            "assets_dir": str(run_dir / "assets"),
            "work_dir": str(run_dir / "work"),
            "rows_dir": str(run_dir / "work" / "rows"),
            "row_dir": None,
            "batch_dir": str(run_dir / "work" / "batch"),
            "row_index": None,
        },
    )

    assert result == [
        [{"seen": str(run_dir / "work" / "batch" / "a")}],
        [{"seen": str(run_dir / "work" / "batch" / "b")}],
    ]


def test_execution_context_rejects_old_per_row_work_dir(tmp_path):
    run_dir = tmp_path / "run"
    rows_dir = run_dir / "work" / "rows"

    with pytest.raises(ValueError, match="work_dir must be"):
        ExecutionContext(
            run_dir=run_dir,
            assets_dir=run_dir / "assets",
            work_dir=rows_dir / "000000",
            rows_dir=rows_dir,
            row_dir=rows_dir / "000000",
            batch_dir=None,
            row_index="0",
        )


def test_execution_context_rejects_old_batch_work_dir(tmp_path):
    run_dir = tmp_path / "run"
    work_dir = run_dir / "work"

    with pytest.raises(ValueError, match="work_dir must be"):
        ExecutionContext(
            run_dir=run_dir,
            assets_dir=run_dir / "assets",
            work_dir=work_dir / "batch",
            rows_dir=work_dir / "rows",
            row_dir=None,
            batch_dir=work_dir / "batch",
            row_index=None,
        )


def test_worker_loads_tool_package_with_relative_imports(tmp_path):
    for module_name in list(sys.modules):
        if module_name == "tools" or module_name.startswith("tools."):
            sys.modules.pop(module_name, None)

    tools_dir = tmp_path / "tools"
    tools_dir.mkdir()
    (tools_dir / "__init__.py").write_text("", encoding="utf-8")
    (tools_dir / "helpers.py").write_text(
        "def suffix():\n"
        "    return 'ok'\n",
        encoding="utf-8",
    )
    (tools_dir / "pkg_tool.py").write_text(
        """
from typing import Any
from bioimageflow_core import Arguments, EnvironmentSpec, IOModel, ProcessingTool
from .helpers import suffix

env = EnvironmentSpec(name="pkg", dependencies={})

class PackageTool(ProcessingTool):
    environment = env

    class Inputs(IOModel):
        value: str

    class Outputs(IOModel):
        seen: str

    def process_row(self, arguments: Arguments) -> Any:
        return self.Outputs(seen=f"{arguments.value}-{suffix()}")
""",
        encoding="utf-8",
    )
    tool_ref = json.dumps({
        "mode": "module",
        "module": "tools.pkg_tool",
        "sys_path": str(tmp_path),
    })

    result = run_process_row((tool_ref, "PackageTool", {"value": "sample"}))

    assert result == [{"seen": "sample-ok"}]
