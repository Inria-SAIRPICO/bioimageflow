"""
Test workflow serialization (export/import).

Covers:
- Export workflow to JSON
- Import and re-execute
- Serialized format includes graph edges, params, tool refs
- Workflow custom tool source is serialized with exported workflow files
"""

import json
import sys
from typing import Any

import pandas as pd

from bioimageflow import ToolRegistry, Workflow

from .conftest import FileLoader, StubSegmenter


def _write_workflow_tools_package(root):
    tools_dir = root / "tools"
    data_dir = tools_dir / "data"
    data_dir.mkdir(parents=True)
    (tools_dir / "__init__.py").write_text(
        "from .source import LocalSource\n"
        "from .annotate import LocalAnnotate\n",
        encoding="utf-8",
    )
    (tools_dir / "helpers.py").write_text(
        "from pathlib import Path\n\n"
        "def read_label():\n"
        "    return (Path(__file__).parent / 'data' / 'label.txt').read_text().strip()\n",
        encoding="utf-8",
    )
    (tools_dir / "source.py").write_text(
        "from typing import Any\n"
        "import pandas as pd\n"
        "from bioimageflow import DataFrameTool\n"
        "from bioimageflow_core import IOModel\n\n"
        "class LocalSource(DataFrameTool):\n"
        "    display_name = 'Local Source'\n"
        "    class Outputs(IOModel):\n"
        "        value: str\n"
        "    def transform(self, df: Any, arguments: Any) -> Any:\n"
        "        return pd.DataFrame([{'value': 'sample'}])\n",
        encoding="utf-8",
    )
    (tools_dir / "annotate.py").write_text(
        "from typing import Any\n"
        "from bioimageflow import DataFrameTool, Passthrough\n"
        "from .helpers import read_label\n\n"
        "class LocalAnnotate(DataFrameTool):\n"
        "    display_name = 'Local Annotate'\n"
        "    class Outputs(Passthrough):\n"
        "        label: str\n"
        "    def transform(self, df: Any, arguments: Any) -> Any:\n"
        "        out = df.copy()\n"
        "        out['label'] = read_label()\n"
        "        return out\n",
        encoding="utf-8",
    )
    (data_dir / "label.txt").write_text("bundled-label\n", encoding="utf-8")


class TestWorkflowExport:

    def test_export_creates_json_file(self, tmp_workspace):
        load = FileLoader()
        segment = StubSegmenter()

        with Workflow(storage_path=tmp_workspace / "results") as wf:
            raw = load(path=str(tmp_workspace / "data"))
            masks = segment(input_image=raw["path"], diameter=30.0)
            wf.compute(masks)
            wf.export(tmp_workspace / "workflow.json")

            assert (tmp_workspace / "workflow.json").exists()

    def test_exported_format_contains_required_fields(self, tmp_workspace):
        load = FileLoader()
        segment = StubSegmenter()

        with Workflow(storage_path=tmp_workspace / "results") as wf:
            raw = load(path=str(tmp_workspace / "data"))
            masks = segment(input_image=raw["path"], diameter=30.0)
            wf.compute(masks)
            wf.export(tmp_workspace / "workflow.json")

        data = json.loads((tmp_workspace / "workflow.json").read_text())

        # Must contain node definitions
        assert "nodes" in data
        # Must contain edge definitions
        assert "edges" in data
        # Must contain workflow config
        assert "config" in data

    def test_exported_nodes_have_tool_references(self, tmp_workspace):
        load = FileLoader()
        segment = StubSegmenter()

        with Workflow(storage_path=tmp_workspace / "results") as wf:
            raw = load(path=str(tmp_workspace / "data"))
            masks = segment(input_image=raw["path"], diameter=30.0)
            wf.compute(masks)
            wf.export(tmp_workspace / "workflow.json")

        data = json.loads((tmp_workspace / "workflow.json").read_text())
        for node in data["nodes"]:
            # Each node must reference its tool class
            assert "tool_module" in node
            assert "tool_class" in node

    def test_export_embeds_custom_tool_modules(self, tmp_workspace):
        load = FileLoader()
        segment = StubSegmenter()

        with Workflow(storage_path=tmp_workspace / "results") as wf:
            raw = load(path=str(tmp_workspace / "data"))
            segment(input_image=raw["path"], diameter=30.0)
            wf.export(tmp_workspace / "workflow.json")

        data = json.loads((tmp_workspace / "workflow.json").read_text())

        assert "custom_tool_modules" in data
        assert data["custom_tool_modules"]
        source_ids = {record["id"] for record in data["custom_tool_modules"]}
        assert source_ids
        for record in data["custom_tool_modules"]:
            assert record["source"]
            assert record["source_hash"]
            assert record["filename"].endswith(".py")
        for node in data["nodes"]:
            assert node["tool_source_module"] in source_ids

    def test_export_bundles_workflow_tools_directory(self, tmp_workspace):
        _write_workflow_tools_package(tmp_workspace)
        sys.path.insert(0, str(tmp_workspace))
        try:
            from tools import LocalAnnotate, LocalSource

            with Workflow(storage_path=tmp_workspace / "results") as wf:
                source = LocalSource()()
                LocalAnnotate()(source)
                wf.export(tmp_workspace / "workflow.json")
        finally:
            sys.path = [p for p in sys.path if p != str(tmp_workspace)]
            for name in [n for n in sys.modules if n == "tools" or n.startswith("tools.")]:
                del sys.modules[name]

        data = json.loads((tmp_workspace / "workflow.json").read_text())
        bundle = next(record for record in data["custom_tool_modules"] if "files" in record)
        paths = {file_record["path"] for file_record in bundle["files"]}

        assert bundle["root_package"] == "tools"
        assert "tools/__init__.py" in paths
        assert "tools/helpers.py" in paths
        assert "tools/data/label.txt" in paths


class TestWorkflowImport:

    def test_load_and_reexecute(self, tmp_workspace):
        load = FileLoader()
        segment = StubSegmenter()
        results: list[Any] = []

        with Workflow(storage_path=tmp_workspace / "results") as wf:
            raw = load(path=str(tmp_workspace / "data"))
            masks = segment(input_image=raw["path"], diameter=30.0)
            results.append(wf.compute(masks))
            wf.export(tmp_workspace / "workflow.json")

        # Reload and re-execute
        loaded = Workflow.load(tmp_workspace / "workflow.json")
        # Find the terminal node by name
        terminal = loaded.nodes["StubSegmenter_1"]
        results.append(loaded.compute(terminal))

        pd.testing.assert_frame_equal(results[0], results[1])

    def test_load_uses_embedded_custom_tool_source(self, tmp_workspace):
        load = FileLoader()
        segment = StubSegmenter()

        with Workflow(storage_path=tmp_workspace / "results") as wf:
            raw = load(path=str(tmp_workspace / "data"))
            segment(input_image=raw["path"], diameter=30.0)
            wf.export(tmp_workspace / "workflow.json")

        loaded = Workflow.load(tmp_workspace / "workflow.json")

        assert type(
            loaded.nodes["FileLoader_1"].tool
        ).__module__.startswith("bioimageflow_custom_tools_")
        assert type(
            loaded.nodes["StubSegmenter_1"].tool
        ).__module__.startswith("bioimageflow_custom_tools_")

    def test_load_uses_bundled_tools_directory_imports_and_data(self, tmp_workspace):
        _write_workflow_tools_package(tmp_workspace)
        sys.path.insert(0, str(tmp_workspace))
        try:
            from tools import LocalAnnotate, LocalSource

            with Workflow(storage_path=tmp_workspace / "results") as wf:
                source = LocalSource()()
                annotated = LocalAnnotate()(source)
                wf.export(tmp_workspace / "workflow.json")

            expected = wf.compute(annotated)
        finally:
            sys.path = [p for p in sys.path if p != str(tmp_workspace)]
            for name in [n for n in sys.modules if n == "tools" or n.startswith("tools.")]:
                del sys.modules[name]

        loaded = Workflow.load(tmp_workspace / "workflow.json")
        result = loaded.compute(loaded.nodes["LocalAnnotate_1"])

        pd.testing.assert_frame_equal(result, expected)
        assert result["label"].tolist() == ["bundled-label"]
        assert type(
            loaded.nodes["LocalAnnotate_1"].tool
        ).__module__.startswith("bioimageflow_custom_tools_")


class TestWorkflowCustomToolRegistry:
    def test_register_workflow_discovers_live_custom_tools(self, tmp_workspace):
        load = FileLoader()
        segment = StubSegmenter()

        with Workflow(storage_path=tmp_workspace / "results") as wf:
            raw = load(path=str(tmp_workspace / "data"))
            segment(input_image=raw["path"], diameter=30.0)

            reg = ToolRegistry()
            metas = reg.register_workflow(wf)

        names = {meta.class_name for meta in metas}
        assert {"FileLoader", "StubSegmenter"}.issubset(names)
        assert reg.get_class("FileLoader") is not None

    def test_register_workflow_discovers_exported_custom_tools(self, tmp_workspace):
        load = FileLoader()
        segment = StubSegmenter()

        with Workflow(storage_path=tmp_workspace / "results") as wf:
            raw = load(path=str(tmp_workspace / "data"))
            segment(input_image=raw["path"], diameter=30.0)
            wf.export(tmp_workspace / "workflow.json")

        data = json.loads((tmp_workspace / "workflow.json").read_text())
        reg = ToolRegistry()
        metas = reg.register_workflow(data)

        names = {meta.class_name for meta in metas}
        assert {"FileLoader", "StubSegmenter"}.issubset(names)
        assert reg.get_class("StubSegmenter") is not None
