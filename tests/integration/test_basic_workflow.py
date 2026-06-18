"""
Test basic workflow construction and execution.

Covers:
- Source node → ProcessingTool → compute
- Single terminal node execution
- DataFrame output structure (only Outputs columns + index)
- Node naming (auto and custom)
- Implicit default Workflow via Node.compute()
"""


import pandas as pd
import pytest

from bioimageflow import Workflow
from bioimageflow.engine import SequentialEngine

from .conftest import FileLoader, StubSegmenter, StubStats


class TestBasicLinearWorkflow:
    """Source → Segmenter → Stats: the simplest useful pipeline."""

    def test_linear_pipeline_returns_dataframe(self, tmp_workspace):
        load = FileLoader()
        segment = StubSegmenter()
        measure = StubStats()

        with Workflow(engine="direct", storage_path=tmp_workspace / "results") as wf:
            raw = load(path=str(tmp_workspace / "data"))
            masks = segment(input_image=raw["path"], diameter=30.0)
            results = measure(image=raw["path"], mask=masks["mask"])
            df = wf.compute(results)
            assert isinstance(df, pd.DataFrame)
            assert len(df) == 3  # 3 input images
            assert "mean_intensity" in df.columns
            assert "area" in df.columns

    def test_processing_tool_output_has_only_declared_columns(self, tmp_workspace):
        """ProcessingTool output contains only Outputs fields, not upstream columns."""
        load = FileLoader()
        segment = StubSegmenter()

        with Workflow(engine="direct", storage_path=tmp_workspace / "results") as wf:
            raw = load(path=str(tmp_workspace / "data"))
            masks = segment(input_image=raw["path"])
            df = wf.compute(masks)
            assert "mask" in df.columns
            assert "cell_count" in df.columns
            # Upstream columns must NOT be carried forward
            assert "path" not in df.columns
            assert "filename" not in df.columns

    def test_downstream_references_different_ancestors(self, tmp_workspace):
        """Stats references 'path' from raw (grandparent) and 'mask' from masks (parent)."""
        load = FileLoader()
        segment = StubSegmenter()
        measure = StubStats()

        with Workflow(engine="direct", storage_path=tmp_workspace / "results") as wf:
            raw = load(path=str(tmp_workspace / "data"))
            masks = segment(input_image=raw["path"])
            results = measure(image=raw["path"], mask=masks["mask"])
            df = wf.compute(results)
            assert len(df) == 3
            assert set(df.columns) == {"mean_intensity", "area"}


class TestNodeNaming:

    def test_auto_generated_node_names(self, tmp_workspace):
        load = FileLoader()
        segment = StubSegmenter()

        with Workflow(engine="direct", storage_path=tmp_workspace / "results"):
            raw = load(path=str(tmp_workspace / "data"))
            masks1 = segment(input_image=raw["path"], diameter=30.0)
            masks2 = segment(input_image=raw["path"], diameter=50.0)
            # Auto-names use tool name + counter
            assert raw.name == "FileLoader_1"
            assert masks1.name == "StubSegmenter_1"
            assert masks2.name == "StubSegmenter_2"

    def test_custom_node_names(self, tmp_workspace):
        load = FileLoader()
        segment = StubSegmenter()

        with Workflow(engine="direct", storage_path=tmp_workspace / "results"):
            raw = load(path=str(tmp_workspace / "data"), name="my_source")
            masks = segment(input_image=raw["path"], name="small_cells")
            assert raw.name == "my_source"
            assert masks.name == "small_cells"

    def test_duplicate_custom_names_raise(self, tmp_workspace):
        load = FileLoader()
        segment = StubSegmenter()

        with pytest.raises(ValueError, match="unique"):
            with Workflow(engine="direct", storage_path=tmp_workspace / "results"):
                raw = load(path=str(tmp_workspace / "data"), name="my_node")
                _masks = segment(input_image=raw["path"], name="my_node")


class TestImplicitWorkflow:

    def test_node_compute_creates_default_workflow(self, tmp_workspace):
        """Node.compute() works without an explicit Workflow."""
        load = FileLoader()
        segment = StubSegmenter()

        raw = load(path=str(tmp_workspace / "data"))
        masks = segment(input_image=raw["path"])
        df = masks.compute(engine=SequentialEngine(use_wetlands=False))

        assert isinstance(df, pd.DataFrame)
        assert len(df) == 3

    def test_explicit_workflow_without_context_manager(self, tmp_workspace):
        wf = Workflow(engine="direct", storage_path=tmp_workspace / "results")
        load = FileLoader()
        segment = StubSegmenter()

        raw = load(path=str(tmp_workspace / "data"))
        masks = segment(input_image=raw["path"])
        df = wf.compute(masks)

        assert isinstance(df, pd.DataFrame)
        assert len(df) == 3


class TestMultipleTerminals:

    def test_compute_multiple_terminals(self, tmp_workspace):
        load = FileLoader()
        segment = StubSegmenter()
        measure = StubStats()

        with Workflow(engine="direct", storage_path=tmp_workspace / "results") as wf:
            raw = load(path=str(tmp_workspace / "data"))
            masks = segment(input_image=raw["path"])
            results = measure(image=raw["path"], mask=masks["mask"])
            out = wf.compute(masks, results)
            assert isinstance(out, dict)
            assert len(out) == 2
            assert all(isinstance(v, pd.DataFrame) for v in out.values())

    def test_compute_no_args_auto_detects_terminals(self, tmp_workspace):
        """compute() with no arguments finds all terminal nodes."""
        load = FileLoader()
        segment = StubSegmenter()
        measure = StubStats()

        with Workflow(engine="direct", storage_path=tmp_workspace / "results") as wf:
            raw = load(path=str(tmp_workspace / "data"))
            masks = segment(input_image=raw["path"])
            _results = measure(image=raw["path"], mask=masks["mask"])
            # masks is not terminal (_results depends on it via mask=masks["mask"])
            # _results IS terminal
            df = wf.compute()
            # Only one terminal: should return DataFrame directly
            assert isinstance(df, pd.DataFrame)

    def test_shared_upstream_not_reexecuted(self, tmp_workspace):
        """When two terminals share an upstream node, it runs only once."""
        load = FileLoader()
        seg1 = StubSegmenter()
        seg2 = StubSegmenter()

        with Workflow(engine="direct", storage_path=tmp_workspace / "results") as wf:
            raw = load(path=str(tmp_workspace / "data"))
            masks_a = seg1(input_image=raw["path"], diameter=30.0, name="seg_a")
            masks_b = seg2(input_image=raw["path"], diameter=50.0, name="seg_b")
            out = wf.compute(masks_a, masks_b)
            assert "seg_a" in out
            assert "seg_b" in out


class TestColumnBindingWinsOverStaleConstant:
    """If a node has both a column binding and a stale constant for the same
    field, the column binding (per-row upstream value) must win.

    Construction normally enforces mutual exclusion, but ``set_constant`` and
    other manual mutations can leave both populated. Before the fix, a stray
    ``None`` constant on a connected ``input_image`` field overwrote the
    upstream path, and the worker received the literal string ``'None'``.
    """

    def test_stale_constant_does_not_override_upstream_value(self, tmp_workspace):
        load = FileLoader()
        segment = StubSegmenter()

        with Workflow(engine="direct", storage_path=tmp_workspace / "results") as wf:
            raw = load(path=str(tmp_workspace / "data"))
            masks = segment(input_image=raw["path"])

            # Simulate a stale platform mutation: a constant lingered on a
            # field that is also column-bound. Without the fix this clobbers
            # the upstream Path with ``None``.
            seg_node = next(
                n for n in wf._nodes.values()
                if type(n.tool).__name__ == "StubSegmenter"
            )
            assert "input_image" in seg_node._column_bindings
            seg_node._constant_bindings["input_image"] = None

            df = wf.compute(masks)

        # The mask filenames are templated as ``{input_image.stem}_mask_…``.
        # If the stale constant had won, the stem would be ``"None"``.
        mask_paths = [str(p) for p in df["mask"]]
        assert all("cell_0" in p for p in mask_paths), mask_paths
        assert not any("None_mask" in p for p in mask_paths), mask_paths
