"""Worker-side forwarding of ExecutionContext."""

import json
import sys

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
        return self.Outputs(seen=str(context.work_dir / arguments.value))
"""
    )

    result = run_process_row(
        (
            str(tool_file),
            "ContextTool",
            {"value": "marker"},
            {
                "run_dir": str(tmp_path / "run"),
                "assets_dir": str(tmp_path / "run" / "assets"),
                "work_dir": str(tmp_path / "run" / "work" / "rows" / "000000"),
                "row_index": "sample",
            },
        )
    )

    assert result == [{"seen": str(tmp_path / "run" / "work" / "rows" / "000000" / "marker")}]


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
        return [self.Outputs(seen=str(context.work_dir / args.value)) for args in arguments_list]
"""
    )

    result = run_process_batch(
        str(tool_file),
        "BatchContextTool",
        [{"value": "a"}, {"value": "b"}],
        {
            "run_dir": str(tmp_path / "run"),
            "assets_dir": str(tmp_path / "run" / "assets"),
            "work_dir": str(tmp_path / "run" / "work" / "batch"),
            "row_index": None,
        },
    )

    assert result == [
        [{"seen": str(tmp_path / "run" / "work" / "batch" / "a")}],
        [{"seen": str(tmp_path / "run" / "work" / "batch" / "b")}],
    ]


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
