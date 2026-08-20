"""Tests for segmentation tool wrappers without importing heavy ML packages."""

from __future__ import annotations

import sys
import types
import importlib
import importlib.util
from pathlib import Path
from typing import Any, cast

import imageio.v3 as iio
import numpy as np
import pytest

from bioimageflow_core import Arguments

try:
    segmentation_tools = importlib.import_module("bioimageflow_segmentation_tools")
    cellpose_v3 = importlib.import_module("bioimageflow_segmentation_tools.cellpose_v3")
    stardist_segmenter = importlib.import_module(
        "bioimageflow_segmentation_tools.stardist_segmenter"
    )
except ModuleNotFoundError:
    pytestmark = pytest.mark.skip(
        reason="bioimageflow_segmentation_tools is owned by the segmentation package"
    )
    Cellpose3 = None
    StarDistSegmenter = None
    cellpose_v3_env = None
    stardist_env = None
else:
    Cellpose3 = segmentation_tools.Cellpose3
    StarDistSegmenter = segmentation_tools.StarDistSegmenter
    cellpose_v3_env = cellpose_v3.cellpose_v3_env
    stardist_env = stardist_segmenter.stardist_env


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


class TestCellpose3:
    def test_environment_pins_cellpose_v3(self) -> None:
        env = cellpose_v3_env
        tool_cls = Cellpose3
        assert env is not None
        assert tool_cls is not None
        assert env.name == "segmentation-cellpose-v3"
        assert "cellpose==3.1.1.1" in env.dependencies["pip"]
        assert "packaging==26.2" in env.dependencies["pip"]
        assert tool_cls.environment is env

    def test_process_row_writes_mask(self, tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        image_path = tmp_path / "input.tif"
        mask_path = tmp_path / "mask.tif"
        iio.imwrite(image_path, np.zeros((8, 8), dtype=np.uint8))

        calls = {}

        class FakeCellpose:
            def __init__(self, model_type: str) -> None:
                calls["model_type"] = model_type

            def eval(self, image, **kwargs):  # type: ignore[no-untyped-def]
                calls["shape"] = image.shape
                calls.update(kwargs)
                masks = np.zeros((8, 8), dtype=np.uint32)
                masks[1:3, 1:3] = 1
                masks[5:7, 5:7] = 2
                return masks, None, None, None

        tool_cls = Cellpose3
        assert tool_cls is not None

        fake_cellpose = cast(Any, types.ModuleType("cellpose"))
        fake_cellpose.models = types.SimpleNamespace(Cellpose=FakeCellpose)
        monkeypatch.setitem(sys.modules, "cellpose", fake_cellpose)

        result = tool_cls().process_row(
            Arguments(
                input_image=image_path,
                mask=mask_path,
                diameter=12.0,
                model_type="nuclei",
                channel=0,
                nuclear_channel=0,
                channel_axis="last",
                flow_threshold=0.4,
                cellprob_threshold=0.0,
            )
        )

        assert result.cell_count == 2
        assert calls["model_type"] == "nuclei"
        assert calls["diameter"] == 12.0
        assert calls["channels"] == [0, 0]
        assert calls["channel_axis"] is None
        assert iio.imread(mask_path).max() == 2


class TestStarDistSegmenter:

    def test_process_row_writes_mask(self, tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        image_path = tmp_path / "input.tif"
        mask_path = tmp_path / "mask.tif"
        iio.imwrite(image_path, np.zeros((2, 8, 8), dtype=np.uint8))

        calls = {}

        def fake_normalize(image, low, high, axis=None):  # type: ignore[no-untyped-def]
            calls["normalized_shape"] = image.shape
            calls["normalize"] = (low, high, axis)
            return image

        class FakeStarDist2D:
            @classmethod
            def from_pretrained(cls, model_name: str):  # type: ignore[no-untyped-def]
                calls["model_name"] = model_name
                return cls()

            def predict_instances(self, image, **kwargs):  # type: ignore[no-untyped-def]
                calls["predict_shape"] = image.shape
                calls["predict_kwargs"] = kwargs
                labels = np.zeros((8, 8), dtype=np.uint32)
                labels[1:3, 1:3] = 1
                labels[5:7, 5:7] = 2
                labels[3:5, 3:5] = 3
                return labels, {}

        tool_cls = StarDistSegmenter
        assert tool_cls is not None

        fake_csbdeep = types.ModuleType("csbdeep")
        fake_csbdeep_utils = cast(Any, types.ModuleType("csbdeep.utils"))
        fake_csbdeep_utils.normalize = fake_normalize
        fake_stardist = types.ModuleType("stardist")
        fake_stardist_models = cast(Any, types.ModuleType("stardist.models"))
        fake_stardist_models.StarDist2D = FakeStarDist2D

        monkeypatch.setitem(sys.modules, "csbdeep", fake_csbdeep)
        monkeypatch.setitem(sys.modules, "csbdeep.utils", fake_csbdeep_utils)
        monkeypatch.setitem(sys.modules, "stardist", fake_stardist)
        monkeypatch.setitem(sys.modules, "stardist.models", fake_stardist_models)

        result = tool_cls().process_row(
            Arguments(
                input_image=image_path,
                mask=mask_path,
                model_name="2D_versatile_fluo",
                channel=1,
                channel_axis="first",
                prob_thresh=0.5,
                nms_thresh=0.3,
                normalize_low=1.0,
                normalize_high=99.8,
            )
        )

        assert result.object_count == 3
        assert calls["model_name"] == "2D_versatile_fluo"
        assert calls["normalized_shape"] == (8, 8)
        assert calls["normalize"] == (1.0, 99.8, (0, 1))
        assert calls["predict_kwargs"] == {"prob_thresh": 0.5, "nms_thresh": 0.3}
        assert iio.imread(mask_path).max() == 3

    def test_prepare_image_handles_channel_first_he_rgb(self) -> None:
        tool_cls = StarDistSegmenter
        assert tool_cls is not None
        image = np.zeros((3, 8, 9), dtype=np.uint8)
        prepared = tool_cls._prepare_image(image, "2D_versatile_he", 0, "first")
        assert prepared.shape == (8, 9, 3)

    def test_prepare_image_handles_channel_last_fluorescence(self) -> None:
        tool_cls = StarDistSegmenter
        assert tool_cls is not None
        image = np.zeros((8, 9, 3), dtype=np.uint8)
        prepared = tool_cls._prepare_image(
            image,
            "2D_versatile_fluo",
            2,
            "last",
        )
        assert prepared.shape == (8, 9)
