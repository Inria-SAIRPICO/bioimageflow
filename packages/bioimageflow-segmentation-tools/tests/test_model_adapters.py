"""Fast contract tests for learned segmentation adapters."""

from pathlib import Path
import sys
import types

import imageio.v3 as iio
import numpy as np
import pytest

from bioimageflow_core import Arguments
from bioimageflow_segmentation_tools import InstanSegSegment, Nagini3DSegment


pytestmark = pytest.mark.package_tools


def test_instanseg_selected_target_writes_one_label_mask(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: dict[str, object] = {}

    class FakeInnerModel:
        cells_and_nuclei = True

    class FakeInstanSeg:
        def __init__(self, **kwargs: object) -> None:
            calls["init"] = kwargs
            self.instanseg = FakeInnerModel()

        def eval_small_image(self, image: np.ndarray, **kwargs: object) -> np.ndarray:
            calls["eval"] = kwargs
            labels = np.zeros(image.shape[-2:], dtype=np.int32)
            labels[1:3, 2:4] = 7
            return labels[None, None]

    module = types.ModuleType("instanseg")
    module.InstanSeg = FakeInstanSeg  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "instanseg", module)
    image_path = tmp_path / "image.tif"
    mask_path = tmp_path / "mask.tif"
    provenance_path = tmp_path / "provenance.json"
    iio.imwrite(image_path, np.arange(30, dtype=np.uint16).reshape(5, 6))

    result = InstanSegSegment().process_row(
        Arguments(
            input_image=image_path,
            model_name="fluorescence_nuclei_and_cells",
            model_path=None,
            target="cells",
            pixel_size_um=0.5,
            channel_axis="first",
            channel_ids=None,
            processing_method="small",
            device="cpu",
            mask=mask_path,
            model_provenance=provenance_path,
        )
    )

    assert result.object_count == 1
    assert iio.imread(mask_path).dtype == np.uint32
    assert calls["eval"] == {
        "pixel_size": 0.5,
        "return_image_tensor": False,
        "target": "cells",
    }
    assert calls["init"] == {
        "model_type": "fluorescence_nuclei_and_cells",
        "device": "cpu",
        "image_reader": "skimage.io",
        "verbosity": 0,
    }
    assert '"model_kind": "named"' in provenance_path.read_text()


def test_instanseg_local_model_requires_torchscript_bundle(tmp_path: Path) -> None:
    image_path = tmp_path / "image.tif"
    iio.imwrite(image_path, np.ones((4, 4), dtype=np.uint8))
    empty_bundle = tmp_path / "model"
    empty_bundle.mkdir()

    with pytest.raises(ValueError, match="instanseg.pt"):
        InstanSegSegment().process_row(
            Arguments(
                input_image=image_path,
                model_name="ignored",
                model_path=empty_bundle,
                target="nuclei",
                pixel_size_um=None,
                channel_axis="first",
                channel_ids=None,
                processing_method="small",
                device="cpu",
                mask=tmp_path / "mask.tif",
                model_provenance=tmp_path / "provenance.json",
            )
        )


def test_instanseg_rejects_cells_for_nucleus_only_model(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeInnerModel:
        cells_and_nuclei = False

    class FakeInstanSeg:
        def __init__(self, **_: object) -> None:
            self.instanseg = FakeInnerModel()

    module = types.ModuleType("instanseg")
    module.InstanSeg = FakeInstanSeg  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "instanseg", module)
    image_path = tmp_path / "image.tif"
    iio.imwrite(image_path, np.ones((4, 4), dtype=np.uint8))

    with pytest.raises(ValueError, match="does not provide cell"):
        InstanSegSegment().process_row(
            Arguments(
                input_image=image_path,
                model_name="fluorescence_nuclei_and_cells",
                model_path=None,
                target="cells",
                pixel_size_um=None,
                channel_axis="first",
                channel_ids=None,
                processing_method="small",
                device="cpu",
                mask=tmp_path / "mask.tif",
                model_provenance=tmp_path / "provenance.json",
            )
        )


def test_nagini_writes_labels_probability_surfaces_and_provenance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeCuda:
        @staticmethod
        def is_available() -> bool:
            return False

    torch_module = types.ModuleType("torch")
    torch_module.cuda = FakeCuda()  # type: ignore[attr-defined]
    torch_module.as_tensor = lambda value, device=None: np.asarray(value)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "torch", torch_module)

    class FakeNagini:
        def __init__(self, **_: object) -> None:
            pass

        def load_weights(self, _: str) -> None:
            pass

        def inference(self, image: np.ndarray, **_: object):
            mask = np.zeros(image.shape, dtype=np.int32)
            mask[1:3, 1:3, 1:3] = 1
            return (
                mask,
                np.full(image.shape, 0.75, dtype=np.float32),
                {
                    "points": np.ones((1, 4, 3), dtype=np.float32),
                    "facets": np.array([[0, 1, 2]], dtype=np.int32),
                    "values": np.ones((1, 4), dtype=np.float32),
                    "centers": np.array([[2.0, 2.0, 2.0]], dtype=np.float32),
                    "params": np.ones((1, 4), dtype=np.float32),
                },
            )

    class FakeSampler:
        def __init__(self, **_: object) -> None:
            pass

        def get_curvature_and_position(self, parameters: np.ndarray):
            return np.ones((len(parameters), 3)), np.ones((len(parameters),))

    nagini = types.ModuleType("nagini3D")
    models = types.ModuleType("nagini3D.models")
    model = types.ModuleType("nagini3D.models.model")
    tools = types.ModuleType("nagini3D.models.tools")
    refinement = types.ModuleType("nagini3D.models.tools.refinement")
    snake = types.ModuleType("nagini3D.models.tools.snake_sampler")
    model.Nagini3D = FakeNagini  # type: ignore[attr-defined]
    refinement.image_to_refinement_grad = object()  # type: ignore[attr-defined]
    refinement.image_to_refinement_grad_otsu = object()  # type: ignore[attr-defined]
    snake.SnakeSmoothSampler = FakeSampler  # type: ignore[attr-defined]
    for name, module_value in {
        "nagini3D": nagini,
        "nagini3D.models": models,
        "nagini3D.models.model": model,
        "nagini3D.models.tools": tools,
        "nagini3D.models.tools.refinement": refinement,
        "nagini3D.models.tools.snake_sampler": snake,
    }.items():
        monkeypatch.setitem(sys.modules, name, module_value)

    bundle = tmp_path / "model"
    bundle.mkdir()
    (bundle / "config.yaml").write_text(
        "settings:\n  M1: 2\n  M2: 2\n  r_mean: 4\nmodel: {}\n"
    )
    (bundle / "thresholds.yaml").write_text("prob: 0.4\nnms: 0.3\n")
    (bundle / "best.pkl").write_bytes(b"weights")
    volume_path = tmp_path / "volume.tif"
    iio.imwrite(
        volume_path,
        np.ones((4, 5, 6), dtype=np.uint16),
        photometric="minisblack",
    )
    mask_path = tmp_path / "mask.tif"
    probability_path = tmp_path / "probability.tif"
    surfaces_path = tmp_path / "surfaces.npz"
    provenance_path = tmp_path / "provenance.json"

    result = Nagini3DSegment().process_row(
        Arguments(
            input_volume=volume_path,
            model_bundle=bundle,
            weights_filename="best.pkl",
            probability_threshold=None,
            nms_threshold=None,
            tiles_z=1,
            tiles_y=1,
            tiles_x=1,
            anisotropy_z=1.0,
            anisotropy_y=1.0,
            anisotropy_x=1.0,
            optimize_snakes=True,
            otsu_for_snakes=True,
            device="cpu",
            mask=mask_path,
            probability=probability_path,
            surfaces=surfaces_path,
            model_provenance=provenance_path,
        )
    )

    assert result.object_count == 1
    assert iio.imread(mask_path).dtype == np.uint32
    assert iio.imread(probability_path).dtype == np.float32
    with np.load(surfaces_path) as surfaces:
        assert set(surfaces) == {
            "points",
            "facets",
            "values",
            "centers",
            "params",
            "curvature_positions",
            "curvature_values",
        }
    assert '"model_bundle_sha256"' in provenance_path.read_text()
