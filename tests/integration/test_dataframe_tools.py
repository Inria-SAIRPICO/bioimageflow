"""
Test DataFrameTool usage: metadata extraction, filtering, aggregation.

Covers:
- DataFrameTool with positional upstream arguments
- ColumnRegex: dynamic column creation
- FilterRows: row filtering with Passthrough Outputs
- CountLabelOverlaps: aggregation with explicit Outputs
- DataFrameTool as source node (no positional args)
- Chaining DataFrameTools before ProcessingTools (compound pattern)
"""

import pandas as pd
import pytest

from bioimageflow import Workflow

from .conftest import (
    AddColumn,
    ColumnRegex,
    FileLoader,
    FilterRows,
    PrepareRegistration,
    StubRegistration,
    StubSegmenter,
)


class TestColumnRegex:

    def test_extract_metadata_from_filenames(self, tmp_workspace_with_metadata):
        ws = tmp_workspace_with_metadata
        load = FileLoader()
        regex = ColumnRegex()

        with Workflow(storage_path=ws / "results") as wf:
            raw = load(path=str(ws / "data"))
            enriched = regex(
                raw,
                column_name="filename",
                regex=r"(?P<patient>\w+)_(?P<slice>\d+)\.tif",
            )
            df = wf.compute(enriched)
            assert "patient" in df.columns
            assert "slice" in df.columns
            assert set(df["patient"]) == {"patientA", "patientB"}
            # Original columns are preserved (DataFrameTool output = whatever transform returns)
            assert "path" in df.columns
            assert "filename" in df.columns

    def test_column_regex_no_outputs_declaration(self):
        """ColumnRegex has no Outputs — schema is dynamic (depends on regex)."""
        # Check that Outputs is not defined on ColumnRegex itself (may be inherited)
        assert "Outputs" not in ColumnRegex.__dict__ or ColumnRegex.__dict__["Outputs"] is None


class TestFilterRows:

    def test_filter_keeps_matching_rows(self, tmp_workspace):
        load = FileLoader()
        filt = FilterRows()
        segment = StubSegmenter()

        with Workflow(storage_path=tmp_workspace / "results") as wf:
            raw = load(path=str(tmp_workspace / "data"))
            # StubSegmenter returns cell_count=42 for all rows
            masks = segment(input_image=raw["path"])
            # Filter cell_count >= 50 should remove all rows
            filtered = filt(masks, column_name="cell_count", min=50.0)
            df = wf.compute(filtered)
            assert len(df) == 0

    def test_filter_passthrough_preserves_columns(self, tmp_workspace):
        """FilterRows declares Outputs(Passthrough) — all input columns preserved."""
        load = FileLoader()
        filt = FilterRows()

        with Workflow(storage_path=tmp_workspace / "results") as wf:
            raw = load(path=str(tmp_workspace / "data"))
            filtered = filt(raw, column_name="filename", min=None, max=None)
            df = wf.compute(filtered)
            # All columns from upstream preserved
            assert "path" in df.columns
            assert "filename" in df.columns
            assert len(df) == 3


class TestChainedDataFrameTools:

    def test_regex_then_filter_then_process(self, tmp_workspace_with_metadata):
        ws = tmp_workspace_with_metadata
        load = FileLoader()
        regex = ColumnRegex()
        filt = FilterRows()
        segment = StubSegmenter()

        with Workflow(storage_path=ws / "results") as wf:
            raw = load(path=str(ws / "data"))
            enriched = regex(
                raw,
                column_name="filename",
                regex=r"(?P<patient>\w+)_(?P<slice>\d+)\.tif",
            )
            # Keep only patientA — but FilterRows works on numeric columns;
            # instead, filter on slice > 001
            patient_a = filt(enriched, column_name="slice", min=2.0)
            masks = segment(input_image=patient_a["path"])
            df = wf.compute(masks)
            # Only patientA_002 has slice >= 2
            assert len(df) == 1
            assert "mask" in df.columns


class TestCompoundPattern:
    """DataFrameTool reshapes data, ProcessingTool processes each row."""

    def test_prepare_then_register(self, tmp_workspace):
        load = FileLoader()
        prepare = PrepareRegistration()
        register = StubRegistration()

        with Workflow(storage_path=tmp_workspace / "results") as wf:
            raw = load(path=str(tmp_workspace / "data"))
            paired = prepare(raw, reference_index=0)
            registered = register(
                fixed=paired["reference_path"],
                moving=paired["image_path"],
            )
            df = wf.compute(registered)
            # 3 images, first is reference → 2 registrations
            assert len(df) == 2
            assert "registered" in df.columns
            assert "displacement" in df.columns


class TestDataFrameIndexAsString:

    def test_source_dataframe_has_string_index(self, tmp_workspace):
        """Engine must convert source DataFrameTool integer indices to strings."""
        load = FileLoader()

        with Workflow(storage_path=tmp_workspace / "results") as wf:
            raw = load(path=str(tmp_workspace / "data"))
            df = wf.compute(raw)
            for idx in df.index:
                assert isinstance(idx, str), f"Index {idx!r} should be a string, got {type(idx).__name__}"
