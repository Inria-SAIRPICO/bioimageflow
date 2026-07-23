"""
Test workflow branching and fan-out patterns.

Covers:
- Same upstream, different parameters (fan-out)
- Independent branches with shared upstream
- Only reachable nodes are executed (dead branch pruning)
"""

import pandas as pd

from bioimageflow import Workflow

from tests.testkit.integration_tools import FileLoader, StubSegmenter, StubStats


class TestFanOut:
    """Same upstream node feeding multiple downstream nodes with different params."""

    def test_two_segmentations_different_diameters(self, tmp_workspace):
        load = FileLoader()
        segment = StubSegmenter()

        with Workflow(engine="direct", storage_path=tmp_workspace / "results") as wf:
            raw = load(path=str(tmp_workspace / "data"))
            masks_30 = segment(input_image=raw["path"], diameter=30.0, name="seg_30")
            masks_50 = segment(input_image=raw["path"], diameter=50.0, name="seg_50")
            out = wf.compute(masks_30, masks_50)
            assert len(out) == 2
            assert len(out["seg_30"]) == 3
            assert len(out["seg_50"]) == 3

    def test_branching_with_different_downstream(self, tmp_workspace):
        """Two branches from the same source, each with its own downstream."""
        load = FileLoader()
        segment = StubSegmenter()
        measure = StubStats()

        with Workflow(engine="direct", storage_path=tmp_workspace / "results") as wf:
            raw = load(path=str(tmp_workspace / "data"))
            masks_small = segment(input_image=raw["path"], diameter=10.0, name="small")
            masks_large = segment(input_image=raw["path"], diameter=100.0, name="large")
            stats_small = measure(
                image=raw["path"], mask=masks_small["mask"], name="stats_small"
            )
            stats_large = measure(
                image=raw["path"], mask=masks_large["mask"], name="stats_large"
            )
            out = wf.compute(stats_small, stats_large)
            assert "stats_small" in out
            assert "stats_large" in out
            assert len(out["stats_small"]) == 3
            assert len(out["stats_large"]) == 3


class TestDeadBranchPruning:

    def test_unreachable_branch_not_executed(self, tmp_workspace):
        """When computing only one terminal, unrelated branches are skipped."""
        load = FileLoader()
        segment = StubSegmenter()

        with Workflow(engine="direct", storage_path=tmp_workspace / "results") as wf:
            raw = load(path=str(tmp_workspace / "data"))
            masks_a = segment(input_image=raw["path"], diameter=30.0, name="branch_a")
            _masks_b = segment(input_image=raw["path"], diameter=50.0, name="branch_b")
            # Only compute branch_a — branch_b should NOT be executed
            df = wf.compute(masks_a)
            assert isinstance(df, pd.DataFrame)
            assert len(df) == 3
            # branch_b output directory should not exist (not executed)
            branch_b_dir = tmp_workspace / "results" / "data" / "branch_b"
            assert not branch_b_dir.exists()
