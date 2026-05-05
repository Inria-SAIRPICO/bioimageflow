"""
Test workflow serialization (export/import).

Covers:
- Export workflow to JSON
- Import and re-execute
- Serialized format includes graph edges, params, tool refs
- Workflow custom tool source is serialized with exported workflow files
"""

import json
from typing import Any

import pandas as pd

from bioimageflow import ToolRegistry, Workflow

from .conftest import FileLoader, StubSegmenter


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
