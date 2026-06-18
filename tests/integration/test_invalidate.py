"""Tests for :meth:`Workflow.invalidate`
(plan-platform-boundary-refactor.md Task 6).
"""

from pathlib import Path

import pytest

from bioimageflow import NodePlanStatus, Workflow
from bioimageflow.storage import Storage

from .conftest import FileLoader, StubSegmenter, StubStats


def _invalidated_node_names(invalidated) -> set[str]:
    return {selection.node_name for selection in invalidated}


def _build_chain(tmp_path: Path) -> Workflow:
    src = tmp_path / "files"
    src.mkdir(exist_ok=True)
    (src / "a.txt").write_text("a")
    wf = Workflow(engine="direct", storage_path=tmp_path / "cache")
    with wf:
        load = FileLoader()(path=str(src))
        seg = StubSegmenter()(input_image=load["path"])
        StubStats()(image=load["path"], mask=seg["mask"])
    return wf


def _dataframe_current_exists(wf: Workflow, node_name: str) -> bool:
    entry = wf.plan()[node_name]
    assert entry.final_result_key is not None
    result_key = entry.final_result_key
    return (Storage(wf.storage_path).result_dir(result_key) / "current.json").exists()


def _processing_current_exists(wf: Workflow, node_name: str) -> bool:
    entry = wf.plan()[node_name]
    assert entry.final_result_key is not None
    result_key = entry.final_result_key
    return (Storage(wf.storage_path).result_dir(result_key) / "current.json").exists()


def _current_exists(wf: Workflow, result_key: str) -> bool:
    return (Storage(wf.storage_path).result_dir(result_key) / "current.json").exists()


class TestInvalidate:
    def test_clears_node_and_downstream_cache(self, tmp_path: Path) -> None:
        wf = _build_chain(tmp_path)
        wf.compute()
        before = wf.plan()
        seg_key = before["StubSegmenter_1"].final_result_key
        stats_key = before["StubStats_1"].final_result_key
        assert seg_key is not None
        assert stats_key is not None

        assert _dataframe_current_exists(wf, "FileLoader_1")
        for n in ("StubSegmenter_1", "StubStats_1"):
            assert _processing_current_exists(wf, n)

        cleared = wf.invalidate(["StubSegmenter_1"])
        # Returns the removed selections: segmentation + stats (downstream).
        names = _invalidated_node_names(cleared)
        assert "StubSegmenter_1" in names
        assert "StubStats_1" in names
        assert "FileLoader_1" not in names
        assert all(selection.result_key.startswith("rk_") for selection in cleared)
        assert all(selection.selected_record_id.startswith("rec_") for selection in cleared)

        # Disk reflects the same.
        assert not _current_exists(wf, seg_key)
        assert not _current_exists(wf, stats_key)
        assert _dataframe_current_exists(wf, "FileLoader_1")

    def test_no_cascade(self, tmp_path: Path) -> None:
        wf = _build_chain(tmp_path)
        wf.compute()
        before = wf.plan()
        stats_key = before["StubStats_1"].final_result_key
        assert stats_key is not None
        cleared = wf.invalidate(["StubSegmenter_1"], cascade=False)
        assert _invalidated_node_names(cleared) == {"StubSegmenter_1"}
        # Downstream cache survives.
        assert _current_exists(wf, stats_key)

    def test_pending_downstream_invalidation_uses_metadata_not_logical_signature(self, tmp_path: Path) -> None:
        wf = _build_chain(tmp_path)
        wf.compute()
        before = wf.plan()
        seg_key = before["StubSegmenter_1"].final_result_key
        stats_key = before["StubStats_1"].final_result_key
        assert seg_key is not None
        assert stats_key is not None

        # Make the stats node pending by removing its upstream selection while
        # leaving the stats current pointer in place.
        wf.invalidate(["StubSegmenter_1"], cascade=False)
        pending = wf.plan()["StubStats_1"]
        assert pending.status is NodePlanStatus.PENDING_UPSTREAM
        assert pending.final_result_key is None
        assert _current_exists(wf, stats_key)

        cleared = wf.invalidate(["StubStats_1"], cascade=False)

        assert _invalidated_node_names(cleared) == {"StubStats_1"}
        assert not _current_exists(wf, stats_key)
        assert not _current_exists(wf, seg_key)

    def test_no_cascade_leaves_downstream_pending_when_upstream_selection_is_removed(self, tmp_path: Path) -> None:
        wf = _build_chain(tmp_path)
        wf.compute()
        cleared = wf.invalidate(["FileLoader_1"], cascade=False)
        assert _invalidated_node_names(cleared) == {"FileLoader_1"}

        plan = wf.plan()

        assert plan["FileLoader_1"].selected_record_id is None
        assert plan["StubSegmenter_1"].status is NodePlanStatus.PENDING_UPSTREAM
        assert plan["StubSegmenter_1"].final_result_key is None
        assert plan["StubSegmenter_1"].pending_upstreams == ("FileLoader_1",)

    def test_unknown_node_raises_key_error(self, tmp_path: Path) -> None:
        wf = _build_chain(tmp_path)
        with pytest.raises(KeyError):
            wf.invalidate(["does_not_exist"])

    def test_invalidate_before_compute_returns_empty(
        self, tmp_path: Path,
    ) -> None:
        wf = _build_chain(tmp_path)
        # Nothing has been computed; nothing to clear.
        assert wf.invalidate(["StubSegmenter_1"]) == set()

    def test_recompute_after_invalidate_repopulates_cache(
        self, tmp_path: Path,
    ) -> None:
        wf = _build_chain(tmp_path)
        wf.compute()
        wf.invalidate(["FileLoader_1"])
        wf2 = _build_chain(tmp_path)
        wf2.compute()
        assert _dataframe_current_exists(wf2, "FileLoader_1")
