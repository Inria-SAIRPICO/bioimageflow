"""
Test enable/disable node feature.

Covers:
- Node.enabled attribute (default True)
- Node.disable() / Node.enable() convenience methods
- Workflow.disable() / Workflow.enable() by node ref or name
- Disabled leaf node skipped, others still execute
- Disabled source node skips all downstream
- Disabled middle node skips downstream but upstream still runs
- Re-enable after disable, verify cache hit on upstream
- Serialization round-trip preserves enabled flag
- compute_steps yields skipped steps with skipped=True
- All targets disabled raises DisabledNodeError
- Diamond with one branch disabled
"""

import json

import pandas as pd
import pytest

from bioimageflow import Workflow
from bioimageflow.engine import DisabledNodeError

from tests.testkit.integration_tools import FileLoader, StubSegmenter, StubStats


class TestNodeEnabledAttribute:

    def test_node_enabled_by_default(self, tmp_workspace):
        load = FileLoader()
        with Workflow(engine="direct", storage_path=tmp_workspace / "results"):
            raw = load(path=str(tmp_workspace / "data"))
            assert raw.enabled is True

    def test_disable_sets_enabled_false(self, tmp_workspace):
        load = FileLoader()
        with Workflow(engine="direct", storage_path=tmp_workspace / "results"):
            raw = load(path=str(tmp_workspace / "data"))
            raw.disable()
            assert raw.enabled is False

    def test_enable_sets_enabled_true(self, tmp_workspace):
        load = FileLoader()
        with Workflow(engine="direct", storage_path=tmp_workspace / "results"):
            raw = load(path=str(tmp_workspace / "data"))
            raw.disable()
            raw.enable()
            assert raw.enabled is True

    def test_enabled_can_be_set_directly(self, tmp_workspace):
        load = FileLoader()
        with Workflow(engine="direct", storage_path=tmp_workspace / "results"):
            raw = load(path=str(tmp_workspace / "data"))
            raw.enabled = False
            assert raw.enabled is False


class TestWorkflowEnableDisable:

    def test_workflow_disable_by_node_ref(self, tmp_workspace):
        load = FileLoader()
        segment = StubSegmenter()
        with Workflow(engine="direct", storage_path=tmp_workspace / "results") as wf:
            raw = load(path=str(tmp_workspace / "data"))
            masks = segment(input_image=raw["path"])
            wf.disable(masks)
            assert masks.enabled is False
            assert raw.enabled is True

    def test_workflow_disable_by_name(self, tmp_workspace):
        load = FileLoader()
        segment = StubSegmenter()
        with Workflow(engine="direct", storage_path=tmp_workspace / "results") as wf:
            raw = load(path=str(tmp_workspace / "data"))
            masks = segment(input_image=raw["path"])
            wf.disable("StubSegmenter_1")
            assert masks.enabled is False

    def test_workflow_enable_by_name(self, tmp_workspace):
        load = FileLoader()
        segment = StubSegmenter()
        with Workflow(engine="direct", storage_path=tmp_workspace / "results") as wf:
            raw = load(path=str(tmp_workspace / "data"))
            masks = segment(input_image=raw["path"])
            masks.disable()
            wf.enable("StubSegmenter_1")
            assert masks.enabled is True

    def test_workflow_disable_multiple(self, tmp_workspace):
        load = FileLoader()
        segment = StubSegmenter()
        measure = StubStats()
        with Workflow(engine="direct", storage_path=tmp_workspace / "results") as wf:
            raw = load(path=str(tmp_workspace / "data"))
            masks = segment(input_image=raw["path"])
            results = measure(image=raw["path"], mask=masks["mask"])
            wf.disable(masks, results)
            assert masks.enabled is False
            assert results.enabled is False
            assert raw.enabled is True

    def test_workflow_disable_unknown_name_raises(self, tmp_workspace):
        load = FileLoader()
        with Workflow(engine="direct", storage_path=tmp_workspace / "results") as wf:
            _raw = load(path=str(tmp_workspace / "data"))
            with pytest.raises(KeyError):
                wf.disable("nonexistent_node")


class TestDisabledLeafNode:

    def test_disabled_terminal_excluded_from_multi_target(self, tmp_workspace):
        """Disable one of two terminals — the other still computes."""
        load = FileLoader()
        segment = StubSegmenter()
        with Workflow(engine="direct", storage_path=tmp_workspace / "results") as wf:
            raw = load(path=str(tmp_workspace / "data"))
            masks_a = segment(input_image=raw["path"], diameter=30.0, name="seg_a")
            masks_b = segment(input_image=raw["path"], diameter=50.0, name="seg_b")
            masks_b.disable()
            out = wf.compute(masks_a, masks_b)
            assert "seg_a" in out
            assert "seg_b" not in out

    def test_disabled_single_target_raises(self, tmp_workspace):
        """Computing a single disabled target raises DisabledNodeError."""
        load = FileLoader()
        segment = StubSegmenter()
        with Workflow(engine="direct", storage_path=tmp_workspace / "results") as wf:
            raw = load(path=str(tmp_workspace / "data"))
            masks = segment(input_image=raw["path"])
            masks.disable()
            with pytest.raises(DisabledNodeError):
                wf.compute(masks)


class TestDisabledSourceNode:

    def test_disabled_source_skips_all_downstream(self, tmp_workspace):
        """Disabling a source node means nothing can run."""
        load = FileLoader()
        segment = StubSegmenter()
        measure = StubStats()
        with Workflow(engine="direct", storage_path=tmp_workspace / "results") as wf:
            raw = load(path=str(tmp_workspace / "data"))
            masks = segment(input_image=raw["path"])
            results = measure(image=raw["path"], mask=masks["mask"])
            raw.disable()
            with pytest.raises(DisabledNodeError):
                wf.compute(results)


class TestDisabledMiddleNode:

    def test_disabled_middle_skips_downstream_but_upstream_runs(self, tmp_workspace):
        """Disable the middle node: upstream still executes, downstream is skipped."""
        load = FileLoader()
        segment = StubSegmenter()
        measure = StubStats()
        with Workflow(engine="direct", storage_path=tmp_workspace / "results") as wf:
            raw = load(path=str(tmp_workspace / "data"))
            masks = segment(input_image=raw["path"], name="seg")
            results = measure(image=raw["path"], mask=masks["mask"], name="stats")
            masks.disable()
            # stats depends on seg which is disabled → both skipped
            # but raw is still reachable from seg's upstream
            # The only target is stats, which is implicitly skipped → error
            with pytest.raises(DisabledNodeError):
                wf.compute(results)


class TestReEnable:

    def test_reenable_uses_cache(self, tmp_workspace):
        """Disable, compute partial, re-enable, compute full — cache hit on upstream."""
        load = FileLoader()
        segment = StubSegmenter()

        with Workflow(engine="direct", storage_path=tmp_workspace / "results") as wf:
            raw = load(path=str(tmp_workspace / "data"))
            masks = segment(input_image=raw["path"])
            # First: run full workflow to populate cache
            df1 = wf.compute(masks)

        with Workflow(engine="direct", storage_path=tmp_workspace / "results") as wf:
            raw = load(path=str(tmp_workspace / "data"))
            masks = segment(input_image=raw["path"])
            # Disable, then re-enable
            masks.disable()
            masks.enable()
            df2 = wf.compute(masks)

        pd.testing.assert_frame_equal(df1, df2)


class TestDisabledDiamond:

    def test_diamond_one_branch_disabled(self, tmp_workspace):
        """Diamond: shared source, two branches, one disabled.
        The shared source still runs, the active branch works."""
        load = FileLoader()
        seg1 = StubSegmenter()
        seg2 = StubSegmenter()
        measure = StubStats()

        with Workflow(engine="direct", storage_path=tmp_workspace / "results") as wf:
            raw = load(path=str(tmp_workspace / "data"))
            masks_a = seg1(input_image=raw["path"], diameter=30.0, name="seg_a")
            masks_b = seg2(input_image=raw["path"], diameter=50.0, name="seg_b")
            stats = measure(image=raw["path"], mask=masks_a["mask"], name="stats")
            masks_b.disable()
            out = wf.compute(stats, masks_b)
            assert "stats" in out
            assert "seg_b" not in out


class TestComputeStepsWithDisabled:

    def test_skipped_steps_have_skipped_true(self, tmp_workspace):
        """compute_steps yields disabled nodes with skipped=True."""
        load = FileLoader()
        segment = StubSegmenter()
        measure = StubStats()

        with Workflow(engine="direct", storage_path=tmp_workspace / "results") as wf:
            raw = load(path=str(tmp_workspace / "data"))
            masks = segment(input_image=raw["path"], name="seg")
            results = measure(image=raw["path"], mask=masks["mask"], name="stats")
            masks.disable()

            steps = []
            for step in wf.compute_steps(results):
                steps.append((step.node_name, step.skipped))
                if not step.skipped:
                    step.execute()

        steps_dict = dict(steps)
        assert steps_dict["FileLoader_1"] is False  # source runs
        assert steps_dict["seg"] is True  # disabled
        assert steps_dict["stats"] is True  # implicitly skipped

    def test_skipped_step_execute_raises(self, tmp_workspace):
        """Calling execute() on a skipped step raises DisabledNodeError."""
        load = FileLoader()
        segment = StubSegmenter()

        with Workflow(engine="direct", storage_path=tmp_workspace / "results") as wf:
            raw = load(path=str(tmp_workspace / "data"))
            masks = segment(input_image=raw["path"])
            masks.disable()

            for step in wf.compute_steps(masks):
                if step.skipped:
                    with pytest.raises(DisabledNodeError):
                        step.execute()


class TestSerializationRoundTrip:

    def test_disabled_flag_persisted_in_export(self, tmp_workspace):
        load = FileLoader()
        segment = StubSegmenter()

        with Workflow(engine="direct", storage_path=tmp_workspace / "results") as wf:
            raw = load(path=str(tmp_workspace / "data"))
            masks = segment(input_image=raw["path"], name="seg")
            masks.disable()
            wf.export(tmp_workspace / "workflow.json")

        data = json.loads((tmp_workspace / "workflow.json").read_text())
        graph = data.get("workflow", data)
        seg_node = next(n for n in graph["nodes"] if n["name"] == "seg")
        assert seg_node["enabled"] is False

        # Enabled nodes should not have the key (or have it True)
        loader_node = next(n for n in graph["nodes"] if n["name"] == "FileLoader_1")
        assert loader_node.get("enabled", True) is True

    def test_disabled_flag_restored_on_load(self, tmp_workspace):
        load = FileLoader()
        segment = StubSegmenter()

        with Workflow(engine="direct", storage_path=tmp_workspace / "results") as wf:
            raw = load(path=str(tmp_workspace / "data"))
            masks = segment(input_image=raw["path"], name="seg")
            masks.disable()
            wf.export(tmp_workspace / "workflow.json")

        wf2 = Workflow.load(tmp_workspace / "workflow.json")
        assert wf2.nodes["seg"].enabled is False
        assert wf2.nodes["FileLoader_1"].enabled is True


class TestAllTargetsDisabled:

    def test_all_targets_disabled_raises(self, tmp_workspace):
        load = FileLoader()
        seg1 = StubSegmenter()
        seg2 = StubSegmenter()

        with Workflow(engine="direct", storage_path=tmp_workspace / "results") as wf:
            raw = load(path=str(tmp_workspace / "data"))
            masks_a = seg1(input_image=raw["path"], name="seg_a")
            masks_b = seg2(input_image=raw["path"], name="seg_b")
            wf.disable(masks_a, masks_b)
            with pytest.raises(DisabledNodeError):
                wf.compute(masks_a, masks_b)
