"""Tests for ``NodePlanStatus`` (plan-platform-boundary-refactor.md Task 4).

The status enum lets external callers distinguish ``cached`` from
``out_of_date`` (run before with a different signature) and from
``unexecuted`` (never run) without inspecting the storage layout.
"""

from pathlib import Path

import pytest

from bioimageflow import NodePlan, NodePlanStatus, Workflow

from .conftest import FileLoader, StubSegmenter


class TestNodePlanStatusBasics:
    def test_unexecuted_for_fresh_storage(self, tmp_path: Path) -> None:
        wf = Workflow(storage_path=tmp_path / "cache", use_wetlands=False)
        with wf:
            FileLoader()(path=str(tmp_path))
        plan = wf.plan()
        for entry in plan.values():
            assert entry.status is NodePlanStatus.UNEXECUTED
            # Backwards-compat properties continue to read off status.
            assert entry.cached is False
            assert entry.skipped is False

    def test_cached_after_compute(self, tmp_path: Path) -> None:
        src = tmp_path / "files"
        src.mkdir()
        (src / "a.txt").write_text("a")

        def build() -> Workflow:
            wf = Workflow(storage_path=tmp_path / "cache", use_wetlands=False)
            with wf:
                load = FileLoader()(path=str(src))
                StubSegmenter()(input_image=load["path"], diameter=20.0)
            return wf

        wf1 = build()
        wf1.compute()

        wf2 = build()
        plan = wf2.plan()
        for entry in plan.values():
            assert entry.status is NodePlanStatus.CACHED
            assert entry.cached is True

    def test_out_of_date_after_param_change(self, tmp_path: Path) -> None:
        src = tmp_path / "files"
        src.mkdir()
        (src / "a.txt").write_text("a")

        def build(diameter: float) -> Workflow:
            wf = Workflow(storage_path=tmp_path / "cache", use_wetlands=False)
            with wf:
                load = FileLoader()(path=str(src))
                StubSegmenter()(input_image=load["path"], diameter=diameter)
            return wf

        # First run materializes the cache for diameter=20.0.
        build(20.0).compute()

        # Re-plan with a different diameter — same node has prior runs,
        # but the current sig hash does not match.
        plan = build(99.0).plan()
        seg = plan["StubSegmenter_1"]
        assert seg.status is NodePlanStatus.OUT_OF_DATE
        assert seg.cached is False

    def test_skipped_for_disabled(self, tmp_path: Path) -> None:
        wf = Workflow(storage_path=tmp_path / "cache", use_wetlands=False)
        with wf:
            load = FileLoader()(path=str(tmp_path))
            seg = StubSegmenter()(input_image=load["path"])
        wf.disable(seg)
        plan = wf.plan()
        assert plan[seg.name].status is NodePlanStatus.SKIPPED
        assert plan[seg.name].skipped is True


class TestNodePlanStatusValues:
    def test_str_enum(self) -> None:
        # NodePlanStatus is a string enum so values can be JSON-serialized
        # by GUIs without an extra mapping.
        assert NodePlanStatus.CACHED == "cached"
        assert NodePlanStatus.OUT_OF_DATE == "out_of_date"
        assert NodePlanStatus.UNEXECUTED == "unexecuted"
        assert NodePlanStatus.SKIPPED == "skipped"
