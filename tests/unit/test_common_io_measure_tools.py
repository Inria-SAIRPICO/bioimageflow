from pathlib import Path
import json

import imageio.v3 as iio
import numpy as np
import pandas as pd

from bioimageflow.validation import serialize_input_schema, serialize_output_schema
from bioimageflow import Workflow
from bioimageflow_core import Arguments


def test_common_exports_exclude_moved_heavy_tools() -> None:
    import bioimageflow_common_tools as common

    for name in [
        "ConvertImage",
        "CellposeSAM",
        "Cellpose3",
        "StarDistSegmenter",
        "Atlas",
    ]:
        assert not hasattr(common, name)

    assert hasattr(common, "Files")
    assert hasattr(common, "ExtractChannel")
    assert hasattr(common, "LabelOverlaps")


def test_io_tools_schema_and_synthetic_execution(tmp_path: Path) -> None:
    from bioimageflow_io_tools import (
        ReadImage,
        SelectDimensions,
        WriteOmeTiff,
        WriteOmeZarr,
    )

    source = tmp_path / "source.tif"
    data = np.arange(2 * 3 * 4 * 5, dtype=np.uint16).reshape(2, 3, 4, 5)
    iio.imwrite(source, data)

    for tool in [ReadImage, SelectDimensions, WriteOmeTiff, WriteOmeZarr]:
        assert serialize_input_schema(tool)
        assert serialize_output_schema(tool)

    schema = serialize_input_schema(SelectDimensions)
    assert schema["input_image"]["image_spec"]["layouts"] == ["CZYX", "TCYX", "TZYX"]
    assert schema["layout"]["default"] == "CZYX"
    assert schema["channel"]["required"] is False

    read_output = tmp_path / "read.tif"
    read = ReadImage().process_row(
        Arguments(input_image=source, output_image=read_output)
    )
    assert read.output_image == read_output
    np.testing.assert_array_equal(iio.imread(read_output), data)

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
    ome_tiff = WriteOmeTiff().process_row(
        Arguments(
            input_image=selected_output,
            output_image=ome_tiff_output,
            dimension_order="YX",
        )
    )
    assert ome_tiff.output_image == ome_tiff_output
    assert tifffile_shape(ome_tiff_output) == (4, 5)

    zarr_output = tmp_path / "selected.ome.zarr"
    ome_zarr = WriteOmeZarr().process_row(
        Arguments(input_image=selected_output, output_path=zarr_output)
    )
    assert ome_zarr.output_path == zarr_output
    assert (zarr_output / ".zgroup").exists()
    assert (zarr_output / ".zattrs").exists()
    assert (zarr_output / "0" / ".zarray").exists()
    assert (zarr_output / "0" / "0.0").exists()


def test_select_dimensions_uses_declared_axis_layout(tmp_path: Path) -> None:
    from bioimageflow_io_tools import SelectDimensions

    data = np.arange(2 * 3 * 4 * 5, dtype=np.uint16).reshape(2, 3, 4, 5)
    source = tmp_path / "source.tif"
    iio.imwrite(source, data)

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


def test_write_ome_tiff_records_axes_metadata(tmp_path: Path) -> None:
    import tifffile

    from bioimageflow_io_tools import WriteOmeTiff

    source = tmp_path / "czyx.tif"
    output = tmp_path / "czyx.ome.tiff"
    data = np.arange(2 * 3 * 4 * 5, dtype=np.uint16).reshape(2, 3, 4, 5)
    iio.imwrite(source, data)

    WriteOmeTiff().process_row(
        Arguments(input_image=source, output_image=output, dimension_order="CZYX")
    )

    with tifffile.TiffFile(output) as tif:
        assert tif.series[0].axes == "CZYX"
        assert tif.series[0].shape == data.shape


def test_io_and_measurement_packages_are_not_exported_as_custom_sources(
    tmp_path: Path,
) -> None:
    from bioimageflow_io_tools import ReadImage
    from bioimageflow_measurement_tools import CountLabels

    with Workflow(storage_path=tmp_path / "results", use_wetlands=False) as wf:
        read = ReadImage()(input_image=tmp_path / "image.tif", name="read_image")
        CountLabels()(label_image=read["output_image"], name="count_labels")
        wf.export(tmp_path / "workflow.json")

    data = json.loads((tmp_path / "workflow.json").read_text())
    assert "custom_tool_modules" not in data
    assert {node["tool_module"] for node in data["nodes"]} == {
        "bioimageflow_io_tools.image_io",
        "bioimageflow_measurement_tools.measurements",
    }


def test_measurement_tools_schema_and_synthetic_execution(tmp_path: Path) -> None:
    from bioimageflow_measurement_tools import (
        CountLabels,
        IntensityProperties,
        LabelBenchmark,
        RegionProperties,
        SummarizeTable,
    )

    labels = np.zeros((5, 6), dtype=np.uint16)
    labels[1:3, 1:4] = 1
    labels[3:5, 2:5] = 2
    intensity = np.arange(labels.size, dtype=np.float32).reshape(labels.shape)

    labels_path = tmp_path / "labels.tif"
    intensity_path = tmp_path / "intensity.tif"
    iio.imwrite(labels_path, labels)
    iio.imwrite(intensity_path, intensity)

    for tool in [
        RegionProperties,
        IntensityProperties,
        CountLabels,
        SummarizeTable,
        LabelBenchmark,
    ]:
        assert serialize_input_schema(tool)
        assert serialize_output_schema(tool) is not None

    assert "area" in serialize_output_schema(RegionProperties)

    regions = RegionProperties().process_row(Arguments(label_image=labels_path))
    assert [(row.label, row.area) for row in regions] == [(1, 6), (2, 6)]
    assert regions[0].bbox_min_y == 1
    assert regions[0].bbox_max_x == 3

    measurements = IntensityProperties().process_row(
        Arguments(label_image=labels_path, intensity_image=intensity_path)
    )
    assert [row.label for row in measurements] == [1, 2]
    assert measurements[0].mean_intensity == float(intensity[labels == 1].mean())

    counts = CountLabels().process_row(Arguments(label_image=labels_path))
    assert counts.label_count == 2
    assert counts.object_pixel_count == 12

    table = pd.DataFrame(
        {
            "sample": ["a", "a", "b"],
            "area": [1.0, 3.0, 5.0],
            "score": [2.0, 4.0, 6.0],
        }
    )
    summary = SummarizeTable().transform(
        table,
        Arguments(group_by="sample", columns="area,score"),
    )
    assert list(summary.columns) == [
        "sample",
        "area_count",
        "area_mean",
        "area_min",
        "area_max",
        "area_sum",
        "score_count",
        "score_mean",
        "score_min",
        "score_max",
        "score_sum",
    ]
    assert summary.loc[summary["sample"] == "a", "area_mean"].item() == 2.0

    predicted = tmp_path / "predicted.tif"
    np_predicted = labels.copy()
    np_predicted[0, 0] = 3
    iio.imwrite(predicted, np_predicted)
    benchmark = LabelBenchmark().process_row(
        Arguments(predicted_label_image=predicted, reference_label_image=labels_path)
    )
    assert benchmark.predicted_label_count == 3
    assert benchmark.reference_label_count == 2
    assert benchmark.true_positive_pixels == 12


def tifffile_shape(path: Path) -> tuple[int, ...]:
    import tifffile

    with tifffile.TiffFile(path) as tif:
        return tif.series[0].shape
