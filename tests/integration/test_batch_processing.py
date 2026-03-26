"""
Test batch processing via process_batch.

Covers:
- process_batch with 1-to-1 outputs (list[Outputs], auto-wrapped)
- process_batch with 1-to-N outputs (list[list[Outputs]])
- Batch override detection (engine checks type(tool).process_batch)
"""


from bioimageflow import Workflow
from bioimageflow_core import ProcessingTool

from .conftest import FileLoader, StubBatchExploder, StubBatchProcessor


class TestBatchOneToOne:

    def test_batch_processor_runs_all_rows(self, tmp_workspace):
        load = FileLoader()
        batch = StubBatchProcessor()

        with Workflow(storage_path=tmp_workspace / "results") as wf:
            raw = load(path=str(tmp_workspace / "data"))
            embeddings = batch(input_image=raw["path"])
            df = wf.compute(embeddings)
            assert len(df) == 3
            assert "embedding" in df.columns

    def test_batch_output_files_exist(self, tmp_workspace):
        load = FileLoader()
        batch = StubBatchProcessor()

        with Workflow(storage_path=tmp_workspace / "results") as wf:
            raw = load(path=str(tmp_workspace / "data"))
            embeddings = batch(input_image=raw["path"])
            df = wf.compute(embeddings)
            from pathlib import Path

            for _, row in df.iterrows():
                assert Path(row["embedding"]).exists()


class TestBatchOneToN:

    def test_batch_exploder_produces_multiple_outputs(self, tmp_workspace):
        load = FileLoader()
        exploder = StubBatchExploder()

        with Workflow(storage_path=tmp_workspace / "results") as wf:
            raw = load(path=str(tmp_workspace / "data"))
            crops = exploder(input_image=raw["path"])
            df = wf.compute(crops)
            # 3 images × 2 crops each = 6 rows
            assert len(df) == 6
            assert "crop" in df.columns
            # Index should be exploded
            for idx in df.index:
                assert "::" in str(idx)


class TestBatchOverrideDetection:

    def test_batch_tool_is_detected_as_batch(self):
        """Engine detects process_batch override."""
        assert type(StubBatchProcessor()).process_batch is not ProcessingTool.process_batch

    def test_row_tool_is_not_batch(self):
        from .conftest import StubSegmenter
        assert type(StubSegmenter()).process_batch is ProcessingTool.process_batch
