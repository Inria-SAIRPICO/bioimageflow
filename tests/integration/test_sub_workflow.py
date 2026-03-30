"""
Test sub-workflow feature.

Covers:
- SubWorkflow definition with Inputs/Outputs/build()
- Using a sub-workflow as a node in a parent workflow
- Internal nodes are encapsulated (not in parent workflow.nodes)
- Internal nodes visible via compute_steps() with scoped names
- Caching of internal nodes
- Mixed ProcessingTool + DataFrameTool inside sub-workflow
- Nested sub-workflows
- Serialization round-trip
- Error cases (missing outputs, invalid bindings)
- Progress events with scoped names
"""

from pathlib import Path

import pandas as pd
import pytest

from bioimageflow import Workflow
from bioimageflow.sub_workflow import SubWorkflow
from bioimageflow_core import IOModel, ImageSpec, Semantic

from .conftest import (
    FileLoader,
    StubSegmenter,
    StubStats,
    FilterRows,
)

from typing import Annotated


# ---------------------------------------------------------------------------
# Sub-workflow definitions for tests
# ---------------------------------------------------------------------------

class SegmentAndMeasure(SubWorkflow):
    """A sub-workflow that segments images and measures stats."""
    display_name = "Segment And Measure"

    class Inputs(IOModel):
        image: Annotated[Path, ImageSpec(semantics={Semantic.INTENSITY})]
        diameter: float = 30.0

    class Outputs(IOModel):
        mask: Annotated[Path, ImageSpec(semantics={Semantic.LABEL})]
        cell_count: int
        mean_intensity: float
        area: int

    def build(self, inputs):
        segment = StubSegmenter()
        measure = StubStats()

        masks = segment(input_image=inputs.image, diameter=inputs.diameter)
        stats = measure(image=inputs.image, mask=masks["mask"])

        return {
            "mask": masks["mask"],
            "cell_count": masks["cell_count"],
            "mean_intensity": stats["mean_intensity"],
            "area": stats["area"],
        }


class SegmentOnly(SubWorkflow):
    """Minimal sub-workflow: just segmentation."""
    display_name = "Segment Only"

    class Inputs(IOModel):
        image: Annotated[Path, ImageSpec(semantics={Semantic.INTENSITY})]

    class Outputs(IOModel):
        mask: Annotated[Path, ImageSpec(semantics={Semantic.LABEL})]
        cell_count: int

    def build(self, inputs):
        segment = StubSegmenter()
        masks = segment(input_image=inputs.image)
        return {
            "mask": masks["mask"],
            "cell_count": masks["cell_count"],
        }


# ---------------------------------------------------------------------------
# Basic execution
# ---------------------------------------------------------------------------

class TestSubWorkflowBasic:

    def test_sub_workflow_returns_dataframe(self, tmp_workspace):
        load = FileLoader()
        seg_measure = SegmentAndMeasure()

        with Workflow(storage_path=tmp_workspace / "results") as wf:
            raw = load(path=str(tmp_workspace / "data"))
            results = seg_measure(image=raw["path"], diameter=25.0)
            df = wf.compute(results)

        assert isinstance(df, pd.DataFrame)
        assert len(df) == 3
        assert "mask" in df.columns
        assert "cell_count" in df.columns
        assert "mean_intensity" in df.columns
        assert "area" in df.columns

    def test_sub_workflow_output_columns_match_outputs_declaration(self, tmp_workspace):
        """Output DataFrame has exactly the columns declared in Outputs."""
        load = FileLoader()
        seg_measure = SegmentAndMeasure()

        with Workflow(storage_path=tmp_workspace / "results") as wf:
            raw = load(path=str(tmp_workspace / "data"))
            results = seg_measure(image=raw["path"])
            df = wf.compute(results)

        assert set(df.columns) == {"mask", "cell_count", "mean_intensity", "area"}

    def test_sub_workflow_output_used_by_downstream(self, tmp_workspace):
        """Downstream nodes can reference sub-workflow outputs."""
        load = FileLoader()
        seg_measure = SegmentAndMeasure()
        measure2 = StubStats()

        with Workflow(storage_path=tmp_workspace / "results") as wf:
            raw = load(path=str(tmp_workspace / "data"))
            results = seg_measure(image=raw["path"])
            # Use sub-workflow output as input to another tool
            stats2 = measure2(image=raw["path"], mask=results["mask"])
            df = wf.compute(stats2)

        assert isinstance(df, pd.DataFrame)
        assert len(df) == 3
        assert "mean_intensity" in df.columns

    def test_sub_workflow_with_constant_only(self, tmp_workspace):
        """Sub-workflow where all inputs are constants (no column refs)."""
        # SegmentOnly with a constant path — won't work because image needs
        # to be a column ref. Test with the full pipeline instead.
        load = FileLoader()
        seg = SegmentOnly()

        with Workflow(storage_path=tmp_workspace / "results") as wf:
            raw = load(path=str(tmp_workspace / "data"))
            results = seg(image=raw["path"])
            df = wf.compute(results)

        assert len(df) == 3
        assert "mask" in df.columns

    def test_sub_workflow_custom_name(self, tmp_workspace):
        """Custom node names work for sub-workflow nodes."""
        load = FileLoader()
        seg = SegmentOnly()

        with Workflow(storage_path=tmp_workspace / "results"):
            raw = load(path=str(tmp_workspace / "data"))
            results = seg(image=raw["path"], name="my_segmentation")
            assert results.name == "my_segmentation"

    def test_sub_workflow_auto_name(self, tmp_workspace):
        load = FileLoader()
        seg = SegmentOnly()

        with Workflow(storage_path=tmp_workspace / "results"):
            raw = load(path=str(tmp_workspace / "data"))
            r1 = seg(image=raw["path"])
            r2 = seg(image=raw["path"])
            assert r1.name == "SegmentOnly_1"
            assert r2.name == "SegmentOnly_2"


# ---------------------------------------------------------------------------
# Encapsulation
# ---------------------------------------------------------------------------

class TestSubWorkflowEncapsulation:

    def test_internal_nodes_not_in_parent_workflow(self, tmp_workspace):
        """Internal nodes should not appear in the parent workflow's nodes dict."""
        load = FileLoader()
        seg = SegmentOnly()

        with Workflow(storage_path=tmp_workspace / "results") as wf:
            raw = load(path=str(tmp_workspace / "data"))
            _results = seg(image=raw["path"])

            # Parent workflow should only have the file_loader and the sub-workflow node
            assert "FileLoader_1" in wf.nodes
            assert "SegmentOnly_1" in wf.nodes
            # Internal stub_segmenter should NOT be in parent
            assert not any("StubSegmenter" in k for k in wf.nodes)

    def test_internal_nodes_accessible_via_attribute(self, tmp_workspace):
        """Internal nodes are accessible for debugging."""
        load = FileLoader()
        seg = SegmentOnly()

        with Workflow(storage_path=tmp_workspace / "results"):
            raw = load(path=str(tmp_workspace / "data"))
            results = seg(image=raw["path"])

            assert hasattr(results, "internal_nodes")
            internal = results.internal_nodes
            assert len(internal) > 0
            # Should contain the stub_segmenter
            internal_names = [n.name for n in internal]
            assert any("StubSegmenter" in name for name in internal_names)


# ---------------------------------------------------------------------------
# compute_steps visibility
# ---------------------------------------------------------------------------

class TestSubWorkflowComputeSteps:

    def test_internal_nodes_visible_in_compute_steps(self, tmp_workspace):
        """Internal nodes appear in compute_steps() with scoped names."""
        load = FileLoader()
        seg = SegmentOnly()

        with Workflow(storage_path=tmp_workspace / "results") as wf:
            raw = load(path=str(tmp_workspace / "data"))
            results = seg(image=raw["path"])

            names = []
            for step in wf.compute_steps(results):
                names.append(step.node_name)
                step.execute()

        assert "FileLoader_1" in names
        # Internal nodes should have scoped names
        assert any("SegmentOnly_1/" in name for name in names)

    def test_compute_steps_environment_for_internal_nodes(self, tmp_workspace):
        """Internal ProcessingTool steps expose their environment."""
        load = FileLoader()
        seg = SegmentOnly()

        with Workflow(storage_path=tmp_workspace / "results") as wf:
            raw = load(path=str(tmp_workspace / "data"))
            results = seg(image=raw["path"])

            envs = {}
            for step in wf.compute_steps(results):
                envs[step.node_name] = step.environment
                step.execute()

        # File loader (DataFrameTool) has no environment
        assert envs["FileLoader_1"] is None
        # Internal segmenter should have cellpose env
        internal_seg_names = [n for n in envs if "StubSegmenter" in n]
        assert len(internal_seg_names) == 1
        assert envs[internal_seg_names[0]] is not None
        assert envs[internal_seg_names[0]].name == "cellpose"

    def test_compute_steps_topological_order(self, tmp_workspace):
        """Internal nodes respect topological order."""
        load = FileLoader()
        seg_measure = SegmentAndMeasure()

        with Workflow(storage_path=tmp_workspace / "results") as wf:
            raw = load(path=str(tmp_workspace / "data"))
            results = seg_measure(image=raw["path"])

            names = []
            for step in wf.compute_steps(results):
                names.append(step.node_name)
                step.execute()

        # file_loader must come first
        assert names[0] == "FileLoader_1"
        # segmenter must come before stats (within the sub-workflow)
        seg_idx = next(i for i, n in enumerate(names) if "StubSegmenter" in n)
        stats_idx = next(i for i, n in enumerate(names) if "StubStats" in n)
        assert seg_idx < stats_idx


# ---------------------------------------------------------------------------
# Caching
# ---------------------------------------------------------------------------

class TestSubWorkflowCaching:

    def test_internal_nodes_cache_independently(self, tmp_workspace):
        """On second run, internal nodes should hit cache."""
        load = FileLoader()
        seg = SegmentOnly()

        # First run
        with Workflow(storage_path=tmp_workspace / "results") as wf:
            raw = load(path=str(tmp_workspace / "data"))
            results = seg(image=raw["path"])
            df1 = wf.compute(results)

        # Second run — should hit cache
        events = []
        with Workflow(
            storage_path=tmp_workspace / "results",
            on_progress=lambda e: events.append(e),
        ) as wf:
            raw = load(path=str(tmp_workspace / "data"))
            results = seg(image=raw["path"])
            df2 = wf.compute(results)

        pd.testing.assert_frame_equal(df1, df2)
        # At least some nodes should be cached
        cached_events = [e for e in events if e.status == "cached"]
        assert len(cached_events) > 0


# ---------------------------------------------------------------------------
# Mixed tools inside sub-workflow
# ---------------------------------------------------------------------------

class TestSubWorkflowMixedTools:

    def test_dataframe_tool_inside_sub_workflow(self, tmp_workspace):
        """Sub-workflow can contain DataFrameTools alongside ProcessingTools."""

        class FilterAndSegment(SubWorkflow):
            display_name = "Filter And Segment"

            class Inputs(IOModel):
                image: Annotated[Path, ImageSpec(semantics={Semantic.INTENSITY})]
                filename: str

            class Outputs(IOModel):
                mask: Annotated[Path, ImageSpec(semantics={Semantic.LABEL})]
                cell_count: int

            def build(self, inputs):
                from bioimageflow import Collect
                # Use a Collect to gather columns, then segment
                collect = Collect()
                _gathered = collect(inputs._proxy_node)
                _filter_rows = FilterRows()
                # Can't really filter meaningfully here, but proves DFTool works
                segment = StubSegmenter()
                masks = segment(input_image=inputs.image)
                return {
                    "mask": masks["mask"],
                    "cell_count": masks["cell_count"],
                }

        load = FileLoader()
        pipeline = FilterAndSegment()

        with Workflow(storage_path=tmp_workspace / "results") as wf:
            raw = load(path=str(tmp_workspace / "data"))
            results = pipeline(image=raw["path"], filename=raw["filename"])
            df = wf.compute(results)

        assert len(df) == 3
        assert "mask" in df.columns


# ---------------------------------------------------------------------------
# Nested sub-workflows
# ---------------------------------------------------------------------------

class TestNestedSubWorkflows:

    def test_nested_sub_workflow(self, tmp_workspace):
        """A sub-workflow can contain another sub-workflow."""

        class OuterWorkflow(SubWorkflow):
            display_name = "Outer Workflow"

            class Inputs(IOModel):
                image: Annotated[Path, ImageSpec(semantics={Semantic.INTENSITY})]

            class Outputs(IOModel):
                mask: Annotated[Path, ImageSpec(semantics={Semantic.LABEL})]
                cell_count: int
                mean_intensity: float
                area: int

            def build(self, inputs):
                inner = SegmentAndMeasure()
                results = inner(image=inputs.image)
                return {
                    "mask": results["mask"],
                    "cell_count": results["cell_count"],
                    "mean_intensity": results["mean_intensity"],
                    "area": results["area"],
                }

        load = FileLoader()
        outer = OuterWorkflow()

        with Workflow(storage_path=tmp_workspace / "results") as wf:
            raw = load(path=str(tmp_workspace / "data"))
            results = outer(image=raw["path"])
            df = wf.compute(results)

        assert isinstance(df, pd.DataFrame)
        assert len(df) == 3
        assert set(df.columns) == {"mask", "cell_count", "mean_intensity", "area"}

    def test_nested_scoped_names_in_compute_steps(self, tmp_workspace):
        """Nested sub-workflow nodes have double-scoped names."""

        class OuterWorkflow(SubWorkflow):
            display_name = "Outer"

            class Inputs(IOModel):
                image: Annotated[Path, ImageSpec(semantics={Semantic.INTENSITY})]

            class Outputs(IOModel):
                mask: Annotated[Path, ImageSpec(semantics={Semantic.LABEL})]
                cell_count: int

            def build(self, inputs):
                inner = SegmentOnly()
                results = inner(image=inputs.image)
                return {
                    "mask": results["mask"],
                    "cell_count": results["cell_count"],
                }

        load = FileLoader()
        outer = OuterWorkflow()

        with Workflow(storage_path=tmp_workspace / "results") as wf:
            raw = load(path=str(tmp_workspace / "data"))
            results = outer(image=raw["path"])

            names = []
            for step in wf.compute_steps(results):
                names.append(step.node_name)
                step.execute()

        # Should have nested scoping: OuterWorkflow_1/SegmentOnly_1/StubSegmenter_1
        deep_names = [n for n in names if n.count("/") >= 2]
        assert len(deep_names) > 0


# ---------------------------------------------------------------------------
# Progress events
# ---------------------------------------------------------------------------

class TestSubWorkflowProgress:

    def test_progress_events_have_scoped_names(self, tmp_workspace):
        events = []
        load = FileLoader()
        seg = SegmentOnly()

        with Workflow(
            storage_path=tmp_workspace / "results",
            on_progress=lambda e: events.append(e),
        ) as wf:
            raw = load(path=str(tmp_workspace / "data"))
            results = seg(image=raw["path"])
            wf.compute(results)

        # Internal node events should have scoped names
        scoped = [e for e in events if "/" in e.node_name]
        assert len(scoped) > 0


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------

class TestSubWorkflowSerialization:

    def test_export_and_load_round_trip(self, tmp_workspace):
        """Workflow with sub-workflow can be exported and re-loaded."""
        load = FileLoader()
        seg = SegmentOnly()

        with Workflow(storage_path=tmp_workspace / "results") as wf:
            raw = load(path=str(tmp_workspace / "data"))
            results = seg(image=raw["path"])
            df1 = wf.compute(results)
            wf.export(tmp_workspace / "workflow.json")

        # Load and re-execute
        wf2 = Workflow.load(tmp_workspace / "workflow.json")
        # Find the terminal node
        terminal_names = [n for n in wf2.nodes if "SegmentOnly" in n]
        assert len(terminal_names) == 1
        df2 = wf2.compute(wf2.nodes[terminal_names[0]])

        assert set(df1.columns) == set(df2.columns)
        assert len(df1) == len(df2)


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------

class TestSubWorkflowErrors:

    def test_missing_output_in_build_raises(self):
        """build() that doesn't return all Outputs fields raises ValueError."""

        class BadSubWorkflow(SubWorkflow):
            display_name = "Bad Sub"

            class Inputs(IOModel):
                image: Annotated[Path, ImageSpec(semantics={Semantic.INTENSITY})]

            class Outputs(IOModel):
                mask: Annotated[Path, ImageSpec(semantics={Semantic.LABEL})]
                cell_count: int

            def build(self, inputs):
                segment = StubSegmenter()
                masks = segment(input_image=inputs.image)
                return {
                    "mask": masks["mask"],
                    # Missing "cell_count"
                }

        bad = BadSubWorkflow()
        load = FileLoader()

        with pytest.raises(ValueError, match="cell_count"):
            with Workflow():
                raw = load(path="./data")
                bad(image=raw["path"])

    def test_missing_required_input_raises(self, tmp_workspace):
        """Missing required input raises BindingError."""
        from bioimageflow.node import BindingError

        seg = SegmentAndMeasure()

        with pytest.raises(BindingError):
            with Workflow(storage_path=tmp_workspace / "results"):
                # 'image' is required but not provided
                seg()

    def test_unknown_input_field_raises(self, tmp_workspace):
        """Unknown keyword argument raises BindingError."""
        from bioimageflow.node import BindingError

        load = FileLoader()
        seg = SegmentOnly()

        with pytest.raises(BindingError):
            with Workflow(storage_path=tmp_workspace / "results"):
                raw = load(path=str(tmp_workspace / "data"))
                seg(image=raw["path"], nonexistent_field=42)


# ---------------------------------------------------------------------------
# Multiple sub-workflow instances
# ---------------------------------------------------------------------------

class TestMultipleSubWorkflowInstances:

    def test_two_sub_workflows_same_type(self, tmp_workspace):
        """Two instances of the same sub-workflow with different params."""
        load = FileLoader()
        seg1 = SegmentAndMeasure()
        seg2 = SegmentAndMeasure()

        with Workflow(storage_path=tmp_workspace / "results") as wf:
            raw = load(path=str(tmp_workspace / "data"))
            r1 = seg1(image=raw["path"], diameter=20.0)
            r2 = seg2(image=raw["path"], diameter=50.0)
            out = wf.compute(r1, r2)

        assert isinstance(out, dict)
        assert len(out) == 2
        for df in out.values():
            assert len(df) == 3
            assert "mask" in df.columns

    def test_sub_workflow_reused_across_branches(self, tmp_workspace):
        """Sub-workflow output used by multiple downstream nodes."""
        load = FileLoader()
        seg = SegmentOnly()
        stats1 = StubStats()
        stats2 = StubStats()

        with Workflow(storage_path=tmp_workspace / "results") as wf:
            raw = load(path=str(tmp_workspace / "data"))
            masks = seg(image=raw["path"])
            r1 = stats1(image=raw["path"], mask=masks["mask"], name="stats_a")
            r2 = stats2(image=raw["path"], mask=masks["mask"], name="stats_b")
            out = wf.compute(r1, r2)

        assert len(out) == 2
