"""
Test source nodes (no upstream dependencies).

Covers:
- DataFrameTool as source (FileLoader, CsvLoader)
- ProcessingTool as source (isolated file discovery)
- Source ProcessingTool with 1-to-N output
"""

import pandas as pd
import pytest

from bioimageflow import Workflow

from .conftest import CsvLoader, FileLoader, StubSegmenter, StubSourceProcessingTool


class TestDataFrameToolSource:

    def test_file_loader_source(self, tmp_workspace):
        load = FileLoader()

        with Workflow(storage_path=tmp_workspace / "results") as wf:
            raw = load(path=str(tmp_workspace / "data"))
            df = wf.compute(raw)

            assert len(df) == 3
            assert "path" in df.columns
            assert "filename" in df.columns

    def test_csv_loader_source(self, tmp_workspace_two_sources):
        ws = tmp_workspace_two_sources
        load = CsvLoader()

        with Workflow(storage_path=ws / "results") as wf:
            patients = load(path=str(ws / "patients.csv"))
            df = wf.compute(patients)

            assert set(df.columns) >= {"patient_id", "age", "sex"}
            assert len(df) == 3


class TestProcessingToolSource:

    def test_processing_tool_as_source(self, tmp_workspace):
        """ProcessingTool with only constants acts as a source node."""
        source = StubSourceProcessingTool()
        segment = StubSegmenter()

        with Workflow(storage_path=tmp_workspace / "results") as wf:
            discovered = source(directory=str(tmp_workspace / "data"))
            masks = segment(input_image=discovered["path"])
            df = wf.compute(masks)

            # StubSourceProcessingTool discovers files and returns 1-to-N
            assert len(df) == 3
            assert "mask" in df.columns

    def test_processing_tool_source_output_schema(self, tmp_workspace):
        source = StubSourceProcessingTool()

        with Workflow(storage_path=tmp_workspace / "results") as wf:
            discovered = source(directory=str(tmp_workspace / "data"))
            df = wf.compute(discovered)

            assert "path" in df.columns
            assert "metadata" in df.columns
            assert len(df) == 3
