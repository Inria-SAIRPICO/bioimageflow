"""Tests for segmentation tool wrappers without importing heavy ML packages."""

from __future__ import annotations

import sys
import types
from pathlib import Path

import imageio.v3 as iio
import numpy as np

from bioimageflow_core import Arguments
from bioimageflow_common_tools import Cellpose3, StarDistSegmenter
from bioimageflow_common_tools.cellpose_v3 import cellpose_v3_env
from bioimageflow_common_tools.stardist_segmenter import stardist_env


class TestCellpose3:
    def test_environment_pins_cellpose_v3(self) -> None:
        assert cellpose_v3_env.name == "cellpose-v3-3-1-1"
        assert "cellpose==3.1.1.1" in cellpose_v3_env.dependencies["pip"]
        assert "packaging" in cellpose_v3_env.dependencies["pip"]
        assert Cellpose3.environment is cellpose_v3_env

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

        fake_cellpose = types.ModuleType("cellpose")
        fake_cellpose.models = types.SimpleNamespace(Cellpose=FakeCellpose)
        monkeypatch.setitem(sys.modules, "cellpose", fake_cellpose)

        result = Cellpose3().process_row(
            Arguments(
                input_image=image_path,
                mask=mask_path,
                diameter=12.0,
                model_type="nuclei",
                channel=0,
                nuclear_channel=0,
                flow_threshold=0.4,
                cellprob_threshold=0.0,
            )
        )

        assert result.cell_count == 2
        assert calls["model_type"] == "nuclei"
        assert calls["diameter"] == 12.0
        assert calls["channels"] == [0, 0]
        assert iio.imread(mask_path).max() == 2


class TestStarDistSegmenter:
    def test_environment_pins_stardist(self) -> None:
        assert stardist_env.name == "stardist"
        assert "stardist==0.9.2" in stardist_env.dependencies["pip"]
        assert "tensorflow" in stardist_env.dependencies["pip"]
        assert StarDistSegmenter.environment is stardist_env

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

        fake_csbdeep = types.ModuleType("csbdeep")
        fake_csbdeep_utils = types.ModuleType("csbdeep.utils")
        fake_csbdeep_utils.normalize = fake_normalize
        fake_stardist = types.ModuleType("stardist")
        fake_stardist_models = types.ModuleType("stardist.models")
        fake_stardist_models.StarDist2D = FakeStarDist2D

        monkeypatch.setitem(sys.modules, "csbdeep", fake_csbdeep)
        monkeypatch.setitem(sys.modules, "csbdeep.utils", fake_csbdeep_utils)
        monkeypatch.setitem(sys.modules, "stardist", fake_stardist)
        monkeypatch.setitem(sys.modules, "stardist.models", fake_stardist_models)

        result = StarDistSegmenter().process_row(
            Arguments(
                input_image=image_path,
                mask=mask_path,
                model_name="2D_versatile_fluo",
                channel=1,
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
        image = np.zeros((3, 8, 9), dtype=np.uint8)
        prepared = StarDistSegmenter._prepare_image(image, "2D_versatile_he", 0)
        assert prepared.shape == (8, 9, 3)

    def test_prepare_image_handles_channel_last_fluorescence(self) -> None:
        image = np.zeros((8, 9, 3), dtype=np.uint8)
        prepared = StarDistSegmenter._prepare_image(image, "2D_versatile_fluo", 2)
        assert prepared.shape == (8, 9)
