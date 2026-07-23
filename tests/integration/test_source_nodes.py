"""
Test source nodes (no upstream dependencies).

Covers:
- DataFrameTool as source (FileLoader, CsvLoader)
- ProcessingTool as source (isolated file discovery)
- Source ProcessingTool with 1-to-N output
- accepts_upstream enforcement (catalog source tools)
"""


import pytest

from bioimageflow import SourceToolUpstreamError, Workflow

from tests.testkit.integration_tools import (
    CsvLoader,
    FileLoader,
    StubSegmenter,
    StubSourceProcessingTool,
)


class TestDataFrameToolSource:

    def test_file_loader_source(self, tmp_workspace):
        load = FileLoader()

        with Workflow(engine="direct", storage_path=tmp_workspace / "results") as wf:
            raw = load(path=str(tmp_workspace / "data"))
            df = wf.compute(raw)

            assert len(df) == 3
            assert "path" in df.columns
            assert "filename" in df.columns

    def test_csv_loader_source(self, tmp_workspace_two_sources):
        ws = tmp_workspace_two_sources
        load = CsvLoader()

        with Workflow(engine="direct", storage_path=ws / "results") as wf:
            patients = load(path=str(ws / "patients.csv"))
            df = wf.compute(patients)

            assert set(df.columns) >= {"patient_id", "age", "sex"}
            assert len(df) == 3


class TestProcessingToolSource:

    def test_processing_tool_as_source(self, tmp_workspace):
        """ProcessingTool with only constants acts as a source node."""
        source = StubSourceProcessingTool()
        segment = StubSegmenter()

        with Workflow(engine="direct", storage_path=tmp_workspace / "results") as wf:
            discovered = source(directory=str(tmp_workspace / "data"))
            masks = segment(input_image=discovered["path"])
            df = wf.compute(masks)

            # StubSourceProcessingTool discovers files and returns 1-to-N
            assert len(df) == 3
            assert "mask" in df.columns

    def test_processing_tool_source_output_schema(self, tmp_workspace):
        source = StubSourceProcessingTool()

        with Workflow(engine="direct", storage_path=tmp_workspace / "results") as wf:
            discovered = source(directory=str(tmp_workspace / "data"))
            df = wf.compute(discovered)

            assert "path" in df.columns
            assert "metadata" in df.columns
            assert len(df) == 3


class TestAcceptsUpstreamEnforcement:
    """Catalog source tools (Files, Generate) refuse positional upstream args."""

    def test_files_with_upstream_raises(self, tmp_workspace):
        from bioimageflow_common_tools import Files

        with Workflow(engine="direct", storage_path=tmp_workspace / "results"):
            other = Files()(path=str(tmp_workspace / "data"))
            with pytest.raises(SourceToolUpstreamError) as exc_info:
                Files()(other, path=str(tmp_workspace / "data"))
            assert "Files" in str(exc_info.value)
            assert "accepts_upstream=False" in str(exc_info.value)

    def test_generate_with_upstream_raises(self, tmp_workspace):
        from bioimageflow_common_tools import Files, Generate

        with Workflow(engine="direct", storage_path=tmp_workspace / "results"):
            other = Files()(path=str(tmp_workspace / "data"))
            with pytest.raises(SourceToolUpstreamError):
                Generate()(other, column_name="x", values=[1])

    def test_files_kwargs_only_succeeds(self, tmp_workspace):
        from bioimageflow_common_tools import Files

        with Workflow(engine="direct", storage_path=tmp_workspace / "results") as wf:
            raw = Files()(path=str(tmp_workspace / "data"))
            df = wf.compute(raw)
            assert len(df) == 3
            assert "path" in df.columns

    def test_source_tool_upstream_to_validation_error(self):
        exc = SourceToolUpstreamError("oops")
        err = exc.to_validation_error(node="files_1")
        assert err.kind == "source_tool_upstream"
        assert err.node == "files_1"
        assert "oops" in err.message

    def test_files_get_output_schema_static(self, tmp_workspace):
        """Files declares Outputs(IOModel) → static schema via resolve_outputs."""
        from bioimageflow_common_tools import Files

        with Workflow(engine="direct", storage_path=tmp_workspace / "results"):
            f = Files()(path=str(tmp_workspace / "data"))
            schema = f.get_output_schema()
            assert schema is not None
            assert set(schema.keys()) == {"path"}
