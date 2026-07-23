"""
Test DataFrameTool usage: metadata extraction, filtering, aggregation.

Covers:
- DataFrameTool with positional upstream arguments
- ColumnRegex: dynamic column creation
- FilterRows: row filtering with Passthrough Outputs
- CountLabelOverlaps: aggregation with explicit Outputs
- DataFrameTool as source node (no positional args)
- Chaining DataFrameTools before ProcessingTools (compound pattern)
- resolve_outputs / Node.get_output_schema (dynamic output schema)
"""


import pytest

from bioimageflow import ColumnNotFoundError, Workflow

from tests.testkit.integration_tools import (
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

        with Workflow(engine="direct", storage_path=ws / "results") as wf:
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

        with Workflow(engine="direct", storage_path=tmp_workspace / "results") as wf:
            raw = load(path=str(tmp_workspace / "data"))
            # StubSegmenter returns cell_count=42 for all rows
            masks = segment(input_image=raw["path"])
            # Filter cell_count >= 50 should remove all rows
            filtered = filt(masks, column_name="cell_count", min=50.0)
            df = wf.compute(filtered)
            assert len(df) == 0

    def test_processing_tool_dataframe_output_feeds_dataframe_tool(self, tmp_workspace):
        """A ProcessingTool node's result DataFrame is a positional upstream."""
        load = FileLoader()
        filt = FilterRows()
        segment = StubSegmenter()

        with Workflow(engine="direct", storage_path=tmp_workspace / "results") as wf:
            raw = load(path=str(tmp_workspace / "data"))
            masks = segment(input_image=raw["path"])
            filtered = filt(masks, column_name="cell_count", min=40.0)
            df = wf.compute(filtered)

            assert len(df) == 3
            assert set(df.columns) == {"mask", "cell_count"}

    def test_filter_passthrough_preserves_columns(self, tmp_workspace):
        """FilterRows declares Outputs(Passthrough) — all input columns preserved."""
        load = FileLoader()
        filt = FilterRows()

        with Workflow(engine="direct", storage_path=tmp_workspace / "results") as wf:
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

        with Workflow(engine="direct", storage_path=ws / "results") as wf:
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

        with Workflow(engine="direct", storage_path=tmp_workspace / "results") as wf:
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

        with Workflow(engine="direct", storage_path=tmp_workspace / "results") as wf:
            raw = load(path=str(tmp_workspace / "data"))
            df = wf.compute(raw)
            for idx in df.index:
                assert isinstance(idx, str), f"Index {idx!r} should be a string, got {type(idx).__name__}"


class TestGenerateResolveOutputs:
    """Generate.resolve_outputs returns a column schema derived from inputs."""

    def test_resolve_with_column_name(self):
        from bioimageflow_common_tools import Generate

        out = Generate.resolve_outputs({"column_name": "sensitivity"})
        assert out is not None
        assert "sensitivity" in out
        assert out["sensitivity"]["type"] == "any"

    def test_resolve_with_empty_inputs(self):
        from bioimageflow_common_tools import Generate

        assert Generate.resolve_outputs({}) is None

    def test_resolve_with_none_inputs(self):
        from bioimageflow_common_tools import Generate

        assert Generate.resolve_outputs(None) is None

    def test_construction_time_columnref_validates(self):
        """Generate(column_name="x")["x"] succeeds with no deferral."""
        from bioimageflow_common_tools import Generate

        with Workflow(engine="direct"):
            g = Generate()(column_name="sensitivity", values=[1, 2])
            ref = g["sensitivity"]
            assert ref.column == "sensitivity"

    def test_construction_time_unknown_column_raises(self):
        """Generate(column_name="x")["y"] raises ColumnNotFoundError."""
        from bioimageflow_common_tools import Generate

        with Workflow(engine="direct"):
            g = Generate()(column_name="x", values=[1])
            with pytest.raises(ColumnNotFoundError):
                _ = g["nope"]

    def test_unconfigured_generate_defers(self, tmp_workspace):
        """Generate() with no column_name → schema is None → ColumnRef
        is allowed to be created (deferred to runtime).

        We can't *construct* a Generate node without column_name (it's a
        required Inputs field), so this test covers the case where
        column_name is in the binding context but we ask about a column
        not declared.
        """
        # No way to create Generate without column_name; just ensure that
        # if get_output_schema returns None, __getitem__ does not raise.
        from bioimageflow.dataframe_tool import DataFrameTool
        from bioimageflow_core import IOModel

        class DynamicNoSchema(DataFrameTool):
            display_name = "Dynamic"

            class Inputs(IOModel):
                pass

            @classmethod
            def resolve_outputs(cls, inputs=None):
                return None

        with Workflow(engine="direct"):
            n = DynamicNoSchema()()
            # No schema → no construction-time validation → succeeds.
            ref = n["any_column"]
            assert ref.column == "any_column"


class TestNodeGetOutputSchema:
    """Node.get_output_schema for non-merge tools."""

    def test_files_node_schema(self, tmp_workspace):
        from bioimageflow_common_tools import Files

        with Workflow(engine="direct"):
            f = Files()(path=str(tmp_workspace / "data"))
            schema = f.get_output_schema()
            assert schema is not None
            assert set(schema.keys()) == {"path"}

    def test_processing_tool_static_schema(self):
        with Workflow(engine="direct"):
            from tests.testkit.integration_tools import FileLoader, StubSegmenter

            load = FileLoader()(path="/tmp/x")
            seg = StubSegmenter()(input_image=load["path"])
            schema = seg.get_output_schema()
            assert schema is not None
            assert "mask" in schema
            assert "cell_count" in schema
