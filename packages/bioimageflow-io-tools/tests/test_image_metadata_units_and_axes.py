"""Actual TIFF pixel/unit truth and finite unavailable-reader header controls."""

from types import SimpleNamespace

import numpy as np
import pytest
import tifffile

from bioimageflow_core import Arguments
from bioimageflow_io_tools import ReadImageMetadata
from bioimageflow_io_tools._raster import write_raster
from bioimageflow_io_tools.metadata import inspect_image


pytestmark = pytest.mark.package_tools


def test_ome_numeric_sizes_and_units_have_distinct_public_projections(tmp_path):
    path = tmp_path / "calibrated.ome.tif"
    pixels = np.arange(24, dtype=np.uint16).reshape(2, 3, 4)
    tifffile.imwrite(
        path,
        pixels,
        photometric="minisblack",
        metadata={
            "axes": "ZYX",
            "PhysicalSizeX": 120,
            "PhysicalSizeXUnit": "nm",
            "PhysicalSizeY": 0.25,
            "PhysicalSizeYUnit": "µm",
            "PhysicalSizeZ": 2,
        },
    )
    output = ReadImageMetadata().process_row(Arguments(input_image=path))
    assert output.pixel_sizes == {"X": 120, "Y": 0.25, "Z": 2}
    assert output.pixel_size_units == {"X": "nm", "Y": "µm", "Z": "µm"}
    assert output.axes == "ZYX"
    np.testing.assert_array_equal(tifffile.imread(path), pixels)


def test_missing_ome_calibration_has_no_units(tmp_path):
    path = tmp_path / "uncalibrated.ome.tif"
    tifffile.imwrite(path, np.zeros((3, 4), dtype=np.uint16), metadata={"axes": "YX"})
    metadata = inspect_image(path)
    assert metadata.pixel_sizes == {"X": None, "Y": None, "Z": None}
    assert metadata.pixel_size_units == {"X": None, "Y": None, "Z": None}


@pytest.mark.parametrize("width", [3, 4])
def test_declared_scalar_tiff_axes_preserve_pixels_not_color_samples(tmp_path, width):
    pixels = np.arange(2 * 5 * width, dtype=np.uint16).reshape(2, 5, width)
    path = write_raster(pixels, tmp_path / "scalar.tif", axes="ZYX")
    with tifffile.TiffFile(path) as image:
        assert image.series[0].axes == "ZYX"
        assert image.pages[0].photometric == tifffile.PHOTOMETRIC.MINISBLACK
        np.testing.assert_array_equal(image.asarray(), pixels)
    assert inspect_image(path).axes == "ZYX"


def test_declared_tiff_samples_remain_color(tmp_path):
    pixels = np.arange(2 * 5 * 3, dtype=np.uint8).reshape(2, 5, 3)
    path = write_raster(pixels, tmp_path / "color.tif", axes="YXS")
    with tifffile.TiffFile(path) as image:
        assert image.pages[0].photometric == tifffile.PHOTOMETRIC.RGB
        np.testing.assert_array_equal(image.asarray(), pixels)
    assert inspect_image(path).axes == "YXS"


@pytest.mark.parametrize(
    "shape,header,expected",
    [
        ((2, 3, 4), {"axes": "Z?X", "mode": "RGBA"}, "Z?X"),
        ((2, 3, 4), {"axes": "YX?", "mode": "RGBA"}, "YXS"),
        ((2, 3, 4), {"mode": "RGBA"}, "YXS"),
        ((2, 3, 4), {}, "?YX"),
        ((2, 3), {"mode": "P"}, "YX"),
    ],
)
def test_imageio_header_evidence_preserves_partial_axes(
    monkeypatch, tmp_path, shape, header, expected
):
    import imageio.v3 as iio

    monkeypatch.setattr(
        iio, "improps", lambda _path: SimpleNamespace(shape=shape, dtype=np.uint8)
    )
    monkeypatch.setattr(iio, "immeta", lambda _path: header)
    metadata = inspect_image(tmp_path / "header-only.png")
    assert metadata.axes == expected
    assert metadata.shape == shape
    assert metadata.pixel_size_units == {"X": None, "Y": None, "Z": None}
