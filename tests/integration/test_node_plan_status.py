"""Tests for ``NodePlanStatus`` (plan-platform-boundary-refactor.md Task 4).

The status enum lets external callers distinguish ``cached`` from
``prior_selection_miss`` (another selected result exists for this node)
and from ``unexecuted`` (never run) without inspecting the storage layout.
"""

from pathlib import Path

from bioimageflow import NodePlanStatus, Workflow
from bioimageflow.storage_v1 import StorageV1

from .conftest import FileLoader, StubSegmenter


class TestNodePlanStatusBasics:
    def test_unexecuted_for_fresh_storage(self, tmp_path: Path) -> None:
        wf = Workflow(storage_path=tmp_path / "cache")
        with wf:
            load = FileLoader()(path=str(tmp_path))
            StubSegmenter()(input_image=load["path"])
        plan = wf.plan()
        assert plan["FileLoader_1"].status is NodePlanStatus.UNEXECUTED
        assert plan["FileLoader_1"].final_result_key is not None
        assert plan["FileLoader_1"].selected_record_id is None
        assert plan["FileLoader_1"].pending_upstreams == ()
        assert plan["StubSegmenter_1"].status is NodePlanStatus.PENDING_UPSTREAM
        assert plan["StubSegmenter_1"].final_result_key is None
        assert plan["StubSegmenter_1"].selected_record_id is None
        assert plan["StubSegmenter_1"].pending_upstreams == ("FileLoader_1",)
        for entry in plan.values():
            assert entry.cached is False
            assert entry.skipped is False

    def test_cached_after_compute(self, tmp_path: Path) -> None:
        src = tmp_path / "files"
        src.mkdir()
        (src / "a.txt").write_text("a")

        def build() -> Workflow:
            wf = Workflow(storage_path=tmp_path / "cache")
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
            assert entry.final_result_key is not None
            assert entry.selected_record_id is not None
            assert entry.pending_upstreams == ()

    def test_cached_plan_exposes_selected_record_from_current_pointer(self, tmp_path: Path) -> None:
        src = tmp_path / "files"
        src.mkdir()
        (src / "a.txt").write_text("a")

        wf = Workflow(storage_path=tmp_path / "cache")
        with wf:
            node = FileLoader()(path=str(src))
        wf.compute(node)

        plan = wf.plan()
        entry = plan[node.name]
        assert entry.logical_signature
        assert not hasattr(entry, "sig_hash")
        assert entry.final_result_key is not None
        pointer = StorageV1(wf.storage_path).load_current(entry.final_result_key)
        assert pointer is not None
        assert entry.selected_record_id == pointer.record_id

    def test_prior_selection_miss_after_param_change(self, tmp_path: Path) -> None:
        src = tmp_path / "files"
        src.mkdir()
        (src / "a.txt").write_text("a")

        def build(diameter: float) -> Workflow:
            wf = Workflow(storage_path=tmp_path / "cache")
            with wf:
                load = FileLoader()(path=str(src))
                StubSegmenter()(input_image=load["path"], diameter=diameter)
            return wf

        # First run materializes the cache for diameter=20.0.
        build(20.0).compute()

        # Re-plan with a different diameter: the planned result key has no
        # current selection, but the same node has another selected result.
        plan = build(99.0).plan()
        seg = plan["StubSegmenter_1"]
        assert seg.status is NodePlanStatus.PRIOR_SELECTION_MISS
        assert seg.cached is False
        assert seg.final_result_key is not None
        assert seg.selected_record_id is None
        assert seg.pending_upstreams == ()

    def test_dataframe_tool_prior_selection_miss_after_parameter_change(self, tmp_path: Path) -> None:
        src_a = tmp_path / "files_a"
        src_b = tmp_path / "files_b"
        src_a.mkdir()
        src_b.mkdir()
        (src_a / "a.txt").write_text("a")
        (src_b / "b.txt").write_text("b")

        def build(path: Path) -> Workflow:
            wf = Workflow(storage_path=tmp_path / "cache")
            with wf:
                FileLoader()(path=str(path), name="loader")
            return wf

        build(src_a).compute()
        plan = build(src_b).plan()
        entry = plan["loader"]

        assert entry.status is NodePlanStatus.PRIOR_SELECTION_MISS
        assert entry.final_result_key is not None
        assert entry.selected_record_id is None
        assert entry.pending_upstreams == ()

    def test_skipped_for_disabled(self, tmp_path: Path) -> None:
        wf = Workflow(storage_path=tmp_path / "cache")
        with wf:
            load = FileLoader()(path=str(tmp_path))
            seg = StubSegmenter()(input_image=load["path"])
        wf.disable(seg)
        plan = wf.plan()
        assert plan[seg.name].status is NodePlanStatus.SKIPPED
        assert plan[seg.name].skipped is True
        assert plan[seg.name].final_result_key is None
        assert plan[seg.name].selected_record_id is None


class TestNodePlanStatusValues:
    def test_str_enum(self) -> None:
        # NodePlanStatus is a string enum so values can be JSON-serialized
        # by GUIs without an extra mapping.
        assert NodePlanStatus.CACHED == "cached"
        assert NodePlanStatus.PRIOR_SELECTION_MISS == "prior_selection_miss"
        assert NodePlanStatus.UNEXECUTED == "unexecuted"
        assert NodePlanStatus.SKIPPED == "skipped"
        assert NodePlanStatus.PENDING_UPSTREAM == "pending_upstream"
