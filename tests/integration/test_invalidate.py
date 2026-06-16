"""Tests for :meth:`Workflow.invalidate`
(plan-platform-boundary-refactor.md Task 6).
"""

from pathlib import Path

import pytest

from bioimageflow import Workflow
from bioimageflow.cache import dataframe_v1_result_key, processing_v1_result_key
from bioimageflow.storage_v1 import StorageV1

from .conftest import FileLoader, StubSegmenter, StubStats


def _build_chain(tmp_path: Path) -> Workflow:
    src = tmp_path / "files"
    src.mkdir(exist_ok=True)
    (src / "a.txt").write_text("a")
    wf = Workflow(storage_path=tmp_path / "cache")
    with wf:
        load = FileLoader()(path=str(src))
        seg = StubSegmenter()(input_image=load["path"])
        StubStats()(image=load["path"], mask=seg["mask"])
    return wf


def _dataframe_v1_current_exists(wf: Workflow, node_name: str) -> bool:
    entry = wf.plan()[node_name]
    result_key = dataframe_v1_result_key(node_name, entry.sig_hash)
    return (StorageV1(wf.storage_path).result_dir(result_key) / "current.json").exists()


def _processing_v1_current_exists(wf: Workflow, node_name: str) -> bool:
    entry = wf.plan()[node_name]
    result_key = processing_v1_result_key(node_name, entry.sig_hash)
    return (StorageV1(wf.storage_path).result_dir(result_key) / "current.json").exists()


class TestInvalidate:
    def test_clears_node_and_downstream_cache(self, tmp_path: Path) -> None:
        wf = _build_chain(tmp_path)
        wf.compute()

        assert _dataframe_v1_current_exists(wf, "FileLoader_1")
        for n in ("StubSegmenter_1", "StubStats_1"):
            assert _processing_v1_current_exists(wf, n)

        cleared = wf.invalidate(["StubSegmenter_1"])
        # Returns the set of cleared names — segmentation + stats (downstream).
        assert "StubSegmenter_1" in cleared
        assert "StubStats_1" in cleared
        assert "FileLoader_1" not in cleared

        # Disk reflects the same.
        assert not _processing_v1_current_exists(wf, "StubSegmenter_1")
        assert not _processing_v1_current_exists(wf, "StubStats_1")
        assert _dataframe_v1_current_exists(wf, "FileLoader_1")

    def test_no_cascade(self, tmp_path: Path) -> None:
        wf = _build_chain(tmp_path)
        wf.compute()
        cleared = wf.invalidate(["StubSegmenter_1"], cascade=False)
        assert cleared == {"StubSegmenter_1"}
        # Downstream cache survives.
        assert _processing_v1_current_exists(wf, "StubStats_1")

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
        assert _dataframe_v1_current_exists(wf2, "FileLoader_1")
