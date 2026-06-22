from __future__ import annotations

import sys
import types
from pathlib import Path

import imageio.v3 as iio
import numpy as np
import pytest

from bioimageflow.validation import serialize_input_schema, serialize_output_schema
from bioimageflow_core import Arguments

pytestmark = pytest.mark.package_tools


def test_io_tools_schema_and_synthetic_execution(tmp_path: Path) -> None:
    from bioimageflow_io_tools import (
        BioIOConvertImage,
        ConvertImageFormat,
        ConvertToOmeTiff,
        ConvertToOmeZarr,
        ReadImageMetadata,
        SelectChannel,
        SelectDimensions,
        SelectScene,
        SelectTimepoint,
        SelectZRange,
        ValidateImageLayout,
    )

    source = tmp_path / "source.tif"
    data = np.arange(2 * 3 * 4 * 5, dtype=np.uint16).reshape(2, 3, 4, 5)
    iio.imwrite(source, data, photometric="minisblack")

    for tool in [
        BioIOConvertImage,
        ReadImageMetadata,
        ValidateImageLayout,
        ConvertImageFormat,
        ConvertToOmeTiff,
        ConvertToOmeZarr,
        SelectScene,
        SelectTimepoint,
        SelectChannel,
        SelectZRange,
        SelectDimensions,
    ]:
        assert serialize_input_schema(tool)
        assert serialize_output_schema(tool)

    schema = serialize_input_schema(SelectDimensions)
    assert schema["input_image"]["image_spec"]["layouts"] == ["CZYX", "TCYX", "TZYX"]
    assert schema["layout"]["default"] == "CZYX"
    assert schema["channel"]["required"] is False
    convert_inputs = serialize_input_schema(ConvertImageFormat)
    convert_outputs = serialize_output_schema(ConvertImageFormat)
    assert "output_image" not in convert_inputs
    assert "output_image" in convert_outputs
    bioio_inputs = serialize_input_schema(BioIOConvertImage)
    bioio_outputs = serialize_output_schema(BioIOConvertImage)
    assert bioio_inputs["dim_order"]["default"] == "TCZYX"
    assert "output_image" in bioio_outputs

    selected_output = tmp_path / "selected.tif"
    selected = SelectDimensions().process_row(
        Arguments(
            input_image=source,
            output_image=selected_output,
            layout="CZYX",
            channel=1,
            z=2,
            timepoint=None,
        )
    )
    assert selected.output_image == selected_output
    np.testing.assert_array_equal(iio.imread(selected_output), data[1, 2])

    ome_tiff_output = tmp_path / "selected.ome.tiff"
    ome_tiff = ConvertToOmeTiff().process_row(
        Arguments(
            input_image=selected_output,
            output_image=ome_tiff_output,
            dimension_order="YX",
        )
    )
    assert ome_tiff.output_image == ome_tiff_output
    assert tifffile_shape(ome_tiff_output) == (4, 5)

    zarr_output = tmp_path / "selected.ome.zarr"
    ome_zarr = ConvertToOmeZarr().process_row(
        Arguments(input_image=selected_output, output_image=zarr_output)
    )
    assert ome_zarr.output_image == zarr_output
    assert (zarr_output / ".zgroup").exists()
    assert (zarr_output / ".zattrs").exists()
    assert (zarr_output / "0" / ".zarray").exists()
    assert (zarr_output / "0" / "0.0").exists()


def test_read_image_metadata_reports_shape_dtype_and_axes(tmp_path: Path) -> None:
    import bioimageflow_io_tools

    data = np.arange(2 * 3 * 4 * 5, dtype=np.uint16).reshape(2, 3, 4, 5)
    source = tmp_path / "czyx.tif"
    iio.imwrite(source, data, photometric="minisblack")

    metadata = bioimageflow_io_tools.ReadImageMetadata().process_row(
        Arguments(input_image=source)
    )

    assert metadata.shape == [2, 3, 4, 5]
    assert metadata.dtype == "uint16"
    assert metadata.ndim == 4
    assert metadata.axes == "CZYX"
    assert metadata.channel_names == ["channel_0", "channel_1"]
    assert metadata.pixel_sizes == {"X": None, "Y": None, "Z": None}


def test_read_image_metadata_reports_ome_tiff_pixel_sizes(tmp_path: Path) -> None:
    import tifffile

    import bioimageflow_io_tools

    source = tmp_path / "physical_sizes.ome.tiff"
    data = np.zeros((4, 5), dtype=np.uint16)
    tifffile.imwrite(
        source,
        data,
        ome=True,
        metadata={
            "axes": "YX",
            "PhysicalSizeX": 0.2,
            "PhysicalSizeY": 0.3,
            "PhysicalSizeZ": 0.4,
        },
    )

    metadata = bioimageflow_io_tools.ReadImageMetadata().process_row(
        Arguments(input_image=source)
    )

    assert metadata.shape == [4, 5]
    assert metadata.axes == "YX"
    assert metadata.pixel_sizes == {"X": 0.2, "Y": 0.3, "Z": 0.4}


def test_validate_image_layout_checks_length_required_axes_and_sizes(tmp_path: Path) -> None:
    from bioimageflow_io_tools import ValidateImageLayout

    data = np.zeros((2, 3, 4, 5), dtype=np.uint8)
    source = tmp_path / "tczyx.tif"
    iio.imwrite(source, data, photometric="minisblack")

    result = ValidateImageLayout().process_row(
        Arguments(input_image=source, layout="TCYX", required_axes="TC", min_size=1)
    )
    assert result.valid is True
    assert result.axes == "TCYX"
    assert result.shape == [2, 3, 4, 5]

    with pytest.raises(ValueError, match="requires axis 'Z'"):
        ValidateImageLayout().process_row(
            Arguments(input_image=source, layout="TCYX", required_axes="Z", min_size=1)
        )

    with pytest.raises(ValueError, match="has 3 axes"):
        ValidateImageLayout().process_row(
            Arguments(input_image=source, layout="ZYX", required_axes="", min_size=1)
        )

    with pytest.raises(ValueError, match="unknown axes"):
        ValidateImageLayout().process_row(
            Arguments(input_image=source, layout="ABCD", required_axes="", min_size=1)
        )


def test_convert_image_format_converts_and_selects_before_export(tmp_path: Path) -> None:
    import tifffile

    from bioimageflow_io_tools import ConvertImageFormat

    data = np.arange(2 * 3 * 4 * 5, dtype=np.uint16).reshape(2, 3, 4, 5)
    source = tmp_path / "source.tif"
    iio.imwrite(source, data, photometric="minisblack")

    tiff_output = tmp_path / "converted.tif"
    result = ConvertImageFormat().process_row(
        Arguments(
            input_image=source,
            output_image=tiff_output,
            input_layout="CZYX",
            scene=None,
            channel=1,
            z=2,
            timepoint=None,
            dimension_order=None,
        )
    )
    assert result.output_image == tiff_output
    np.testing.assert_array_equal(iio.imread(tiff_output), data[1, 2])

    ome_tiff_output = tmp_path / "converted.ome.tiff"
    ConvertImageFormat().process_row(
        Arguments(
            input_image=source,
            output_image=ome_tiff_output,
            input_layout="CZYX",
            scene=None,
            channel=1,
            z=2,
            timepoint=None,
            dimension_order="YX",
        )
    )
    with tifffile.TiffFile(ome_tiff_output) as tif:
        assert tif.series[0].axes == "YX"
        np.testing.assert_array_equal(tif.series[0].asarray(), data[1, 2])

    ome_zarr_output = tmp_path / "converted.ome.zarr"
    zarr_result = ConvertImageFormat().process_row(
        Arguments(
            input_image=source,
            output_image=ome_zarr_output,
            input_layout="CZYX",
            scene=None,
            channel=1,
            z=2,
            timepoint=None,
            dimension_order=None,
        )
    )
    assert zarr_result.output_image == ome_zarr_output
    assert (ome_zarr_output / ".zattrs").exists()
    assert (ome_zarr_output / "0" / ".zarray").exists()


def test_bioio_convert_image_uses_bioio_plugins_and_selection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from bioimageflow_io_tools import BioIOConvertImage

    source = tmp_path / "source.czi"
    source.write_text("fake czi")
    output = tmp_path / "converted.ome.tiff"
    data = np.arange(2 * 3 * 4 * 5 * 6, dtype=np.uint16).reshape(2, 3, 4, 5, 6)
    saves: list[tuple[np.ndarray, str, str]] = []

    class FakeBioImage:
        dims = "TCZYX"
        shape = data.shape
        dtype = data.dtype

        def __init__(self, path: Path) -> None:
            self.path = path
            self.scene = None

        def set_scene(self, scene: int) -> None:
            self.scene = scene

        def get_image_data(self, dim_order: str, **dim_kwargs: int) -> np.ndarray:
            assert dim_order == "TCZYX"
            assert dim_kwargs == {"C": 1, "Z": 2, "T": 0}
            return data[0:1, 1:2, 2:3]

    class FakeOmeTiffWriter:
        @staticmethod
        def save(array: np.ndarray, path: str, *, dim_order: str) -> None:
            saves.append((array.copy(), path, dim_order))
            Path(path).write_text("ome tiff")

    class FakeOMEZarrWriter:
        def __init__(self, **kwargs: object) -> None:
            self.kwargs = kwargs

        def write_full_volume(self, array: np.ndarray) -> None:
            Path(str(self.kwargs["store"])).mkdir(parents=True, exist_ok=True)

    class FakeTwoDWriter:
        @staticmethod
        def save(array: np.ndarray, path: str, dim_order: str | None = None) -> None:
            Path(path).write_text(f"{array.shape}:{dim_order}")

    bioio_module = types.ModuleType("bioio")
    bioio_module.BioImage = FakeBioImage
    ome_tiff_writers = types.ModuleType("bioio_ome_tiff.writers")
    ome_tiff_writers.OmeTiffWriter = FakeOmeTiffWriter
    ome_zarr_writers = types.ModuleType("bioio_ome_zarr.writers")
    ome_zarr_writers.OMEZarrWriter = FakeOMEZarrWriter
    imageio_writers = types.ModuleType("bioio_imageio.writers")
    imageio_writers.TwoDWriter = FakeTwoDWriter

    monkeypatch.setitem(sys.modules, "bioio", bioio_module)
    monkeypatch.setitem(sys.modules, "bioio_ome_tiff", types.ModuleType("bioio_ome_tiff"))
    monkeypatch.setitem(sys.modules, "bioio_ome_tiff.writers", ome_tiff_writers)
    monkeypatch.setitem(sys.modules, "bioio_ome_zarr", types.ModuleType("bioio_ome_zarr"))
    monkeypatch.setitem(sys.modules, "bioio_ome_zarr.writers", ome_zarr_writers)
    monkeypatch.setitem(sys.modules, "bioio_imageio", types.ModuleType("bioio_imageio"))
    monkeypatch.setitem(sys.modules, "bioio_imageio.writers", imageio_writers)

    result = BioIOConvertImage().process_row(
        Arguments(
            input_image=source,
            output_image=output,
            dim_order="TCZYX",
            scene=2,
            channel=1,
            z=2,
            timepoint=0,
        )
    )

    assert result.output_image == output
    assert output.read_text() == "ome tiff"
    assert saves
    saved_array, saved_path, saved_dim_order = saves[0]
    assert saved_path == str(output)
    assert saved_dim_order == "YX"
    np.testing.assert_array_equal(saved_array, data[0, 1, 2])


def test_select_scene_supports_ordinary_images_and_tiff_series(tmp_path: Path) -> None:
    import tifffile

    from bioimageflow_io_tools import SelectScene

    ordinary_data = np.arange(4 * 5, dtype=np.uint16).reshape(4, 5)
    ordinary = tmp_path / "ordinary.tif"
    iio.imwrite(ordinary, ordinary_data)

    ordinary_output = tmp_path / "ordinary_scene.tif"
    result = SelectScene().process_row(
        Arguments(input_image=ordinary, scene=0, output_image=ordinary_output)
    )
    assert result.output_image == ordinary_output
    np.testing.assert_array_equal(iio.imread(ordinary_output), ordinary_data)

    multi_series = tmp_path / "multi_series.tif"
    first = np.ones((3, 4), dtype=np.uint16)
    second = np.full((2, 5), 7, dtype=np.uint16)
    with tifffile.TiffWriter(multi_series) as tif:
        tif.write(first)
        tif.write(second)

    scene_output = tmp_path / "scene_1.tif"
    SelectScene().process_row(
        Arguments(input_image=multi_series, scene=1, output_image=scene_output)
    )
    np.testing.assert_array_equal(iio.imread(scene_output), second)

    with pytest.raises(IndexError, match="Scene index 2"):
        SelectScene().process_row(
            Arguments(input_image=ordinary, scene=2, output_image=tmp_path / "bad.tif")
        )


def test_explicit_axis_selectors_slice_declared_layouts(tmp_path: Path) -> None:
    from bioimageflow_io_tools import SelectChannel, SelectTimepoint, SelectZRange

    data = np.arange(2 * 3 * 4 * 5, dtype=np.uint16).reshape(2, 3, 4, 5)
    source = tmp_path / "tczyx.tif"
    iio.imwrite(source, data, photometric="minisblack")

    timepoint_output = tmp_path / "timepoint.tif"
    SelectTimepoint().process_row(
        Arguments(
            input_image=source,
            layout="TCYX",
            timepoint=1,
            output_image=timepoint_output,
        )
    )
    np.testing.assert_array_equal(iio.imread(timepoint_output), data[1])

    channel_output = tmp_path / "channel.tif"
    SelectChannel().process_row(
        Arguments(input_image=source, layout="TCYX", channel=2, output_image=channel_output)
    )
    np.testing.assert_array_equal(iio.imread(channel_output), data[:, 2])

    zyx_data = np.arange(4 * 5 * 6, dtype=np.uint16).reshape(4, 5, 6)
    zyx_source = tmp_path / "zyx.tif"
    iio.imwrite(zyx_source, zyx_data, photometric="minisblack")
    zrange_output = tmp_path / "zrange.tif"
    SelectZRange().process_row(
        Arguments(
            input_image=zyx_source,
            layout="ZYX",
            start_z=1,
            stop_z=3,
            output_image=zrange_output,
        )
    )
    np.testing.assert_array_equal(iio.imread(zrange_output), zyx_data[1:3])

    with pytest.raises(ValueError, match="has no T axis"):
        SelectTimepoint().process_row(
            Arguments(input_image=zyx_source, layout="ZYX", timepoint=0, output_image=tmp_path / "bad.tif")
        )


def test_select_dimensions_uses_declared_axis_layout(tmp_path: Path) -> None:
    from bioimageflow_io_tools import SelectDimensions

    data = np.arange(2 * 3 * 4 * 5, dtype=np.uint16).reshape(2, 3, 4, 5)
    source = tmp_path / "source.tif"
    iio.imwrite(source, data, photometric="minisblack")

    cases = [
        ("CZYX", {"z": 1}, data[:, 1, :, :]),
        ("CZYX", {"channel": 1}, data[1, :, :, :]),
        ("TCYX", {"timepoint": 1, "channel": 2}, data[1, 2, :, :]),
        ("TZYX", {"timepoint": 1, "z": 2}, data[1, 2, :, :]),
    ]
    for layout, selections, expected in cases:
        output = tmp_path / f"{layout}_{'_'.join(selections)}.tif"
        result = SelectDimensions().process_row(
            Arguments(
                input_image=source,
                output_image=output,
                layout=layout,
                channel=selections.get("channel"),
                z=selections.get("z"),
                timepoint=selections.get("timepoint"),
            )
        )
        assert result.output_image == output
        np.testing.assert_array_equal(iio.imread(output), expected)


def test_convert_to_ome_tiff_records_axes_metadata(tmp_path: Path) -> None:
    import tifffile

    from bioimageflow_io_tools import ConvertToOmeTiff

    source = tmp_path / "czyx.tif"
    output = tmp_path / "czyx.ome.tiff"
    data = np.arange(2 * 3 * 4 * 5, dtype=np.uint16).reshape(2, 3, 4, 5)
    iio.imwrite(source, data, photometric="minisblack")

    ConvertToOmeTiff().process_row(
        Arguments(input_image=source, output_image=output, dimension_order="CZYX")
    )

    with tifffile.TiffFile(output) as tif:
        assert tif.series[0].axes == "CZYX"
        assert tif.series[0].shape == data.shape


def test_write_named_ome_tools_are_not_public_workflow_tools() -> None:
    import bioimageflow_io_tools

    assert not hasattr(bioimageflow_io_tools, "WriteOmeTiff")
    assert not hasattr(bioimageflow_io_tools, "WriteOmeZarr")


def test_io_package_all_exports_only_public_tools() -> None:
    import bioimageflow_io_tools

    assert sorted(bioimageflow_io_tools.__all__) == [
        "BioIOConvertImage",
        "ConvertImageFormat",
        "ConvertToOmeTiff",
        "ConvertToOmeZarr",
        "ReadImageMetadata",
        "SelectChannel",
        "SelectDimensions",
        "SelectScene",
        "SelectTimepoint",
        "SelectZRange",
        "ValidateImageLayout",
    ]
    assert "image_io" not in bioimageflow_io_tools.__all__
    assert not hasattr(bioimageflow_io_tools, "ReadImage")


def tifffile_shape(path: Path) -> tuple[int, ...]:
    import tifffile

    with tifffile.TiffFile(path) as tif:
        return tif.series[0].shape
