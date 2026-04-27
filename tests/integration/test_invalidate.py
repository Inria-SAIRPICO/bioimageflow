"""Tests for :meth:`Workflow.invalidate`
(plan-platform-boundary-refactor.md Task 6).
"""

from pathlib import Path

import pytest

from bioimageflow import Workflow
from bioimageflow.storage import get_node_dir

from .conftest import FileLoader, StubSegmenter, StubStats


def _build_chain(tmp_path: Path) -> Workflow:
    src = tmp_path / "files"
    src.mkdir(exist_ok=True)
    (src / "a.txt").write_text("a")
    wf = Workflow(storage_path=tmp_path / "cache", use_wetlands=False)
    with wf:
        load = FileLoader()(path=str(src))
        seg = StubSegmenter()(input_image=load["path"])
        StubStats()(image=load["path"], mask=seg["mask"])
    return wf


class TestInvalidate:
    def test_clears_node_and_downstream_cache(self, tmp_path: Path) -> None:
        wf = _build_chain(tmp_path)
        wf.compute()

        # All three node directories should exist after compute.
        for n in ("FileLoader_1", "StubSegmenter_1", "StubStats_1"):
            assert get_node_dir(wf.storage_path, n).exists()

        cleared = wf.invalidate(["StubSegmenter_1"])
        # Returns the set of cleared names — segmentation + stats (downstream).
        assert "StubSegmenter_1" in cleared
        assert "StubStats_1" in cleared
        assert "FileLoader_1" not in cleared

        # Disk reflects the same.
        assert not get_node_dir(wf.storage_path, "StubSegmenter_1").exists()
        assert not get_node_dir(wf.storage_path, "StubStats_1").exists()
        assert get_node_dir(wf.storage_path, "FileLoader_1").exists()

    def test_no_cascade(self, tmp_path: Path) -> None:
        wf = _build_chain(tmp_path)
        wf.compute()
        cleared = wf.invalidate(["StubSegmenter_1"], cascade=False)
        assert cleared == {"StubSegmenter_1"}
        # Downstream cache survives.
        assert get_node_dir(wf.storage_path, "StubStats_1").exists()

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
        assert get_node_dir(wf.storage_path, "FileLoader_1").exists()
