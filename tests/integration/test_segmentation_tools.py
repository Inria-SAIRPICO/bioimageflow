"""Regression tests for moved segmentation wrappers."""

from __future__ import annotations

import importlib.util


def test_common_package_no_longer_contains_canonical_segmentation_modules() -> None:
    import bioimageflow_common_tools as common_tools

    assert not hasattr(common_tools, "Cellpose3")
    assert not hasattr(common_tools, "CellposeSAM")
    assert not hasattr(common_tools, "StarDistSegmenter")
    assert importlib.util.find_spec("bioimageflow_common_tools.cellpose_v3") is None
    assert importlib.util.find_spec("bioimageflow_common_tools.cellpose_sam") is None
    assert (
        importlib.util.find_spec("bioimageflow_common_tools.stardist_segmenter") is None
    )
