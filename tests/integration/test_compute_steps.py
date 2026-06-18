"""
Test step-by-step workflow execution via compute_steps().

Covers:
- NodeStep object with node_name, prepare(), execute()
- Auto-execution when user doesn't call execute()
- prepare() warms Wetlands environment before execute()
- Topological order and reachability
- Partial iteration (early break) cleans up properly
- Cached nodes are still yielded
- Auto-detection of terminal nodes
- Progress callbacks fire during stepped execution
- dev_mode parameter forwarded correctly
"""

import pandas as pd

from bioimageflow import Workflow

from .conftest import (
    FileLoader,
    StubSegmenter,
    StubStats,
)


class TestNodeStepObject:

    def test_step_has_node_name(self, tmp_workspace):
        """Each yielded step exposes the node name."""
        load = FileLoader()
        segment = StubSegmenter()

        with Workflow(engine="direct", storage_path=tmp_workspace / "results") as wf:
            raw = load(path=str(tmp_workspace / "data"))
            masks = segment(input_image=raw["path"])

            names = []
            for step in wf.compute_steps(masks):
                names.append(step.node_name)
                step.execute()

        assert "FileLoader_1" in names
        assert "StubSegmenter_1" in names

    def test_step_has_tool(self, tmp_workspace):
        """Each step exposes the tool instance."""
        load = FileLoader()
        segment = StubSegmenter()

        with Workflow(engine="direct", storage_path=tmp_workspace / "results") as wf:
            raw = load(path=str(tmp_workspace / "data"))
            masks = segment(input_image=raw["path"])

            tools = {}
            for step in wf.compute_steps(masks):
                tools[step.node_name] = step.tool
                step.execute()

        assert isinstance(tools["FileLoader_1"], FileLoader)
        assert isinstance(tools["StubSegmenter_1"], StubSegmenter)

    def test_execute_returns_dataframe(self, tmp_workspace):
        """step.execute() returns the node's output DataFrame."""
        load = FileLoader()
        segment = StubSegmenter()

        with Workflow(engine="direct", storage_path=tmp_workspace / "results") as wf:
            raw = load(path=str(tmp_workspace / "data"))
            masks = segment(input_image=raw["path"])

            results = {}
            for step in wf.compute_steps(masks):
                df = step.execute()
                results[step.node_name] = df

        assert isinstance(results["FileLoader_1"], pd.DataFrame)
        assert "path" in results["FileLoader_1"].columns
        assert isinstance(results["StubSegmenter_1"], pd.DataFrame)
        assert "mask" in results["StubSegmenter_1"].columns

    def test_execute_idempotent(self, tmp_workspace):
        """Calling execute() twice returns the same DataFrame."""
        load = FileLoader()

        with Workflow(engine="direct", storage_path=tmp_workspace / "results") as wf:
            raw = load(path=str(tmp_workspace / "data"))

            for step in wf.compute_steps(raw):
                df1 = step.execute()
                df2 = step.execute()
                pd.testing.assert_frame_equal(df1, df2)


class TestAutoExecute:

    def test_auto_executes_when_user_skips_execute(self, tmp_workspace):
        """If user doesn't call execute(), the step auto-executes on next iteration."""
        load = FileLoader()
        segment = StubSegmenter()
        measure = StubStats()

        with Workflow(engine="direct", storage_path=tmp_workspace / "results") as wf:
            raw = load(path=str(tmp_workspace / "data"))
            masks = segment(input_image=raw["path"])
            results = measure(image=raw["path"], mask=masks["mask"])

            # Only call execute() on the last step
            last_df = None
            for step in wf.compute_steps(results):
                if step.node_name == "StubStats_1":
                    last_df = step.execute()

        # If intermediate nodes didn't auto-execute, the last would fail
        assert last_df is not None
        assert "mean_intensity" in last_df.columns


class TestPrepare:

    def test_prepare_is_callable(self, tmp_workspace):
        """prepare() can be called before execute() without error."""
        load = FileLoader()
        segment = StubSegmenter()

        with Workflow(engine="direct", storage_path=tmp_workspace / "results") as wf:
            raw = load(path=str(tmp_workspace / "data"))
            masks = segment(input_image=raw["path"])

            for step in wf.compute_steps(masks):
                step.prepare()
                step.execute()

    def test_prepare_optional(self, tmp_workspace):
        """Skipping prepare() works fine — execute() still succeeds."""
        load = FileLoader()
        segment = StubSegmenter()

        with Workflow(engine="direct", storage_path=tmp_workspace / "results") as wf:
            raw = load(path=str(tmp_workspace / "data"))
            masks = segment(input_image=raw["path"])

            for step in wf.compute_steps(masks):
                df = step.execute()
                assert isinstance(df, pd.DataFrame)

    def test_prepare_reports_environment(self, tmp_workspace):
        """For ProcessingTools, the step exposes the environment spec."""
        load = FileLoader()
        segment = StubSegmenter()

        with Workflow(engine="direct", storage_path=tmp_workspace / "results") as wf:
            raw = load(path=str(tmp_workspace / "data"))
            masks = segment(input_image=raw["path"])

            envs = {}
            for step in wf.compute_steps(masks):
                envs[step.node_name] = step.environment
                step.execute()

        # FileLoader is a DataFrameTool — no environment
        assert envs["FileLoader_1"] is None
        # StubSegmenter is a ProcessingTool — has an environment
        assert envs["StubSegmenter_1"] is not None
        assert envs["StubSegmenter_1"].name == "cellpose"


class TestComputeStepsOrder:

    def test_topological_order(self, tmp_workspace):
        """Nodes are yielded in dependency order (upstream before downstream)."""
        load = FileLoader()
        segment = StubSegmenter()
        measure = StubStats()

        with Workflow(engine="direct", storage_path=tmp_workspace / "results") as wf:
            raw = load(path=str(tmp_workspace / "data"))
            masks = segment(input_image=raw["path"])
            results = measure(image=raw["path"], mask=masks["mask"])

            names = []
            for step in wf.compute_steps(results):
                names.append(step.node_name)
                step.execute()

        assert names.index("FileLoader_1") < names.index("StubSegmenter_1")
        assert names.index("StubSegmenter_1") < names.index("StubStats_1")

    def test_all_reachable_nodes_yielded(self, tmp_workspace):
        """Every node in the dependency chain is yielded, not just the target."""
        load = FileLoader()
        segment = StubSegmenter()
        measure = StubStats()

        with Workflow(engine="direct", storage_path=tmp_workspace / "results") as wf:
            raw = load(path=str(tmp_workspace / "data"))
            masks = segment(input_image=raw["path"])
            results = measure(image=raw["path"], mask=masks["mask"])

            names = []
            for step in wf.compute_steps(results):
                names.append(step.node_name)
                step.execute()

        assert len(names) == 3
        assert set(names) == {"FileLoader_1", "StubSegmenter_1", "StubStats_1"}


class TestComputeStepsPartialIteration:

    def test_early_break_does_not_leak(self, tmp_workspace):
        """Breaking out of the generator early should clean up the engine."""
        load = FileLoader()
        segment = StubSegmenter()
        measure = StubStats()

        with Workflow(engine="direct", storage_path=tmp_workspace / "results") as wf:
            raw = load(path=str(tmp_workspace / "data"))
            masks = segment(input_image=raw["path"])
            results = measure(image=raw["path"], mask=masks["mask"])

            for step in wf.compute_steps(results):
                step.execute()
                break  # Only consume the first step

        # Verify we can still run a full compute afterwards.
        with Workflow(engine="direct", storage_path=tmp_workspace / "results") as wf:
            raw = load(path=str(tmp_workspace / "data"))
            masks = segment(input_image=raw["path"])
            df = wf.compute(masks)
            assert isinstance(df, pd.DataFrame)

    def test_early_break_without_execute(self, tmp_workspace):
        """Breaking without calling execute() also cleans up properly."""
        load = FileLoader()
        segment = StubSegmenter()

        with Workflow(engine="direct", storage_path=tmp_workspace / "results") as wf:
            raw = load(path=str(tmp_workspace / "data"))
            masks = segment(input_image=raw["path"])

            for step in wf.compute_steps(masks):
                break  # Don't even call execute


class TestComputeStepsCaching:

    def test_cached_nodes_still_yielded(self, tmp_workspace):
        """On a second run, cached nodes are yielded with their cached DataFrames."""
        load = FileLoader()
        segment = StubSegmenter()

        # First run — populate cache
        with Workflow(engine="direct", storage_path=tmp_workspace / "results") as wf:
            raw = load(path=str(tmp_workspace / "data"))
            masks = segment(input_image=raw["path"])
            wf.compute(masks)

        # Second run — should still yield both nodes
        with Workflow(engine="direct", storage_path=tmp_workspace / "results") as wf:
            raw = load(path=str(tmp_workspace / "data"))
            masks = segment(input_image=raw["path"])

            steps_data = []
            for step in wf.compute_steps(masks):
                df = step.execute()
                steps_data.append((step.node_name, df))

        assert len(steps_data) == 2
        for name, df in steps_data:
            assert isinstance(df, pd.DataFrame)
            assert len(df) > 0


class TestComputeStepsTerminals:

    def test_auto_detect_terminals(self, tmp_workspace):
        """compute_steps() with no target auto-detects terminal nodes."""
        load = FileLoader()
        segment = StubSegmenter()

        with Workflow(engine="direct", storage_path=tmp_workspace / "results") as wf:
            raw = load(path=str(tmp_workspace / "data"))
            _masks = segment(input_image=raw["path"])

            names = []
            for step in wf.compute_steps():
                names.append(step.node_name)
                step.execute()

        assert "FileLoader_1" in names
        assert "StubSegmenter_1" in names

    def test_multiple_terminals(self, tmp_workspace):
        """compute_steps() with multiple targets yields all reachable nodes."""
        load = FileLoader()
        seg1 = StubSegmenter()
        seg2 = StubSegmenter()

        with Workflow(engine="direct", storage_path=tmp_workspace / "results") as wf:
            raw = load(path=str(tmp_workspace / "data"))
            masks_a = seg1(input_image=raw["path"], diameter=30.0, name="seg_a")
            masks_b = seg2(input_image=raw["path"], diameter=50.0, name="seg_b")

            names = []
            for step in wf.compute_steps(masks_a, masks_b):
                names.append(step.node_name)
                step.execute()

        assert "FileLoader_1" in names
        assert "seg_a" in names
        assert "seg_b" in names
        assert names.count("FileLoader_1") == 1


class TestComputeStepsProgress:

    def test_progress_events_fire_during_steps(self, tmp_workspace):
        """Progress callbacks still fire when using compute_steps()."""
        events = []

        load = FileLoader()
        segment = StubSegmenter()

        with Workflow(
            engine="direct",
            storage_path=tmp_workspace / "results",
            on_progress=lambda e: events.append(e),
        ) as wf:
            raw = load(path=str(tmp_workspace / "data"))
            masks = segment(input_image=raw["path"])

            for step in wf.compute_steps(masks):
                step.execute()

        node_names = {e.node_name for e in events}
        assert len(node_names) >= 2


class TestComputeStepsDevMode:

    def test_dev_mode_forwarded(self, tmp_workspace):
        """dev_mode=True is forwarded to the engine."""
        load = FileLoader()
        segment = StubSegmenter()

        with Workflow(engine="direct", storage_path=tmp_workspace / "results") as wf:
            raw = load(path=str(tmp_workspace / "data"))
            masks = segment(input_image=raw["path"])

            steps_data = []
            for step in wf.compute_steps(masks, dev_mode=True):
                df = step.execute()
                steps_data.append((step.node_name, df))
            assert len(steps_data) == 2
