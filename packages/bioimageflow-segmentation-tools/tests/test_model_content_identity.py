"""Public finite adapter identity/refusal controls without learned runtimes."""

import hashlib
import importlib.resources
import json
from pathlib import Path
import sys
import types

import imageio.v3 as iio
import numpy as np
import pytest

from bioimageflow_core import Arguments
from bioimageflow_segmentation_tools import InstanSegSegment, StarDistSegmenter


pytestmark = pytest.mark.package_tools


def _arguments(tmp_path, *, shape=(2, 5), model_path=None, suffix="0"):
    source = tmp_path / "image.tif"
    iio.imwrite(source, np.ones(shape, dtype=np.uint16))
    return Arguments(
        input_image=source,
        model_name="selected",
        model_path=model_path,
        target="nuclei",
        pixel_size_um=None,
        channel_axis="first",
        channel_ids=None,
        processing_method="small",
        device="cpu",
        mask=tmp_path / f"mask-{suffix}.tif",
        model_provenance=tmp_path / f"provenance-{suffix}.json",
    )


def _fake_instanseg(monkeypatch, read_value):
    constructions = []

    class Model:
        def __init__(self, **kwargs):
            self.value = read_value(kwargs["model_type"])
            constructions.append(self.value)
            self.instanseg = types.SimpleNamespace(cells_and_nuclei=False)

        def eval_small_image(self, image, **_kwargs):
            return np.full((1, 1, *image.shape[-2:]), self.value, dtype=np.int32)

    module = types.ModuleType("instanseg")
    module.InstanSeg = Model
    monkeypatch.setitem(sys.modules, "instanseg", module)
    return constructions


@pytest.mark.parametrize("shape", [(1, 5), (5, 1)])
def test_instanseg_preserves_singleton_spatial_dimensions(monkeypatch, tmp_path, shape):
    _fake_instanseg(monkeypatch, lambda _: 7)
    output = InstanSegSegment().process_row(_arguments(tmp_path, shape=shape))
    labels = iio.imread(output.mask)
    assert labels.shape == shape and labels.dtype == np.uint32
    np.testing.assert_array_equal(labels, np.full(shape, 7))
    assert output.object_count == 1


def test_instanseg_local_changed_bytes_replace_cached_model_and_used_digest(
    monkeypatch, tmp_path
):
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    weights = bundle / "instanseg.pt"
    weights.write_bytes(b"1")
    constructions = _fake_instanseg(
        monkeypatch, lambda source: int((Path(source) / "instanseg.pt").read_bytes())
    )
    tool = InstanSegSegment()
    digests = []
    for index, value in enumerate([1, 9, 9]):
        weights.write_text(str(value))
        output = tool.process_row(
            _arguments(tmp_path, model_path=bundle, suffix=str(index))
        )
        np.testing.assert_array_equal(iio.imread(output.mask), np.full((2, 5), value))
        expected = hashlib.sha256(
            b"instanseg.pt" + hashlib.sha256(str(value).encode()).digest()
        ).hexdigest()
        provenance = json.loads(output.model_provenance.read_text())
        assert provenance["model_sha256"] == expected
        digests.append(expected)
    assert constructions == [1, 9]
    assert digests[0] != digests[1] == digests[2]


def test_instanseg_named_provenance_matches_model_acquired_and_warm_reuse(
    monkeypatch, tmp_path
):
    resources = tmp_path / "resources"
    index = resources / "bioimageio_models" / "model-index.json"
    index.parent.mkdir(parents=True)
    for version in [1, 9]:
        directory = tmp_path / "models" / "selected" / str(version)
        directory.mkdir(parents=True)
        (directory / "instanseg.pt").write_text(str(version))
    monkeypatch.setenv("INSTANSEG_BIOIMAGEIO_PATH", str(tmp_path / "models"))
    real_files = importlib.resources.files
    monkeypatch.setattr(
        importlib.resources,
        "files",
        lambda module: resources if module == "instanseg" else real_files(module),
    )
    constructions = _fake_instanseg(
        monkeypatch, lambda _: int(json.loads(index.read_text())[0]["version"])
    )
    tool = InstanSegSegment()
    for iteration, version in enumerate([1, 9, 9]):
        index.write_text(
            json.dumps(
                [
                    {
                        "name": "selected",
                        "version": str(version),
                        "url": f"https://example.invalid/{version}",
                    }
                ]
            )
        )
        output = tool.process_row(_arguments(tmp_path, suffix=str(iteration)))
        np.testing.assert_array_equal(iio.imread(output.mask), np.full((2, 5), version))
        stamp = json.loads(output.model_provenance.read_text())
        assert stamp["model_version"] == str(version)
        assert stamp["model_url"] == f"https://example.invalid/{version}"
        assert (
            stamp["model_sha256"] == hashlib.sha256(str(version).encode()).hexdigest()
        )
    assert constructions == [1, 9]


@pytest.mark.parametrize("threshold", ["prob_thresh", "nms_thresh"])
def test_stardist_invalid_threshold_refuses_before_model_acquisition(
    monkeypatch, tmp_path, threshold
):
    acquisitions = []
    models = types.ModuleType("stardist.models")
    models.StarDist2D = types.SimpleNamespace(
        from_pretrained=lambda *args: acquisitions.append(args)
    )
    monkeypatch.setitem(sys.modules, "stardist", types.ModuleType("stardist"))
    monkeypatch.setitem(sys.modules, "stardist.models", models)
    utils = types.ModuleType("csbdeep.utils")
    utils.normalize = lambda image, *_args, **_kwargs: image
    monkeypatch.setitem(sys.modules, "csbdeep", types.ModuleType("csbdeep"))
    monkeypatch.setitem(sys.modules, "csbdeep.utils", utils)
    source = tmp_path / "image.tif"
    iio.imwrite(source, np.ones((3, 5), dtype=np.uint8))
    arguments = dict(
        input_image=source,
        model_name="2D_versatile_fluo",
        channel=0,
        channel_axis="last",
        prob_thresh=None,
        nms_thresh=None,
        normalize_low=1,
        normalize_high=99,
        mask=tmp_path / "mask.tif",
    )
    arguments[threshold] = 2.0
    with pytest.raises(ValueError, match=threshold):
        StarDistSegmenter().process_row(Arguments(**arguments))
    assert acquisitions == []
    assert not (tmp_path / "mask.tif").exists()
