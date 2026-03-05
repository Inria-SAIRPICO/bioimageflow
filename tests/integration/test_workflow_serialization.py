"""
Test workflow serialization (export/import).

Covers:
- Export workflow to JSON
- Import and re-execute
- Serialized format includes graph edges, params, tool refs
- Tool code is NOT serialized (same packages required)
"""

import json
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from bioimageflow import Workflow

from .conftest import FileLoader, StubSegmenter, StubStats


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
        terminal = loaded.nodes["stub_segmenter_1"]
        results.append(loaded.compute(terminal))

        pd.testing.assert_frame_equal(results[0], results[1])
