"""Execution and graph-construction tests for segmentation package tools."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import imageio.v3 as iio
import numpy as np
import pytest
from skimage.filters import threshold_otsu

from bioimageflow import Workflow
from bioimageflow_core import Arguments
from bioimageflow_segmentation_tools import (
    Cellpose3,
    DistanceWatershedSegment,
    FilterLabels,
    LocalThresholdSegment,
    OtsuThresholdSegment,
    PostprocessLabels,
    SplitTouchingObjects,
    StarDistSegmenter,
    ThresholdSegment,
    WatershedSegment,
)

pytestmark = pytest.mark.package_tools


def test_threshold_segment_labels_synthetic_objects(tmp_path: Path) -> None:
    image_path = tmp_path / "input.tif"
    labels_path = tmp_path / "labels.tif"
    image = np.zeros((8, 8), dtype=np.uint8)
    image[1:3, 1:3] = 10
    image[5:7, 5:7] = 10
    iio.imwrite(image_path, image)

    result = ThresholdSegment().process_row(
        Arguments(input_image=image_path, labels=labels_path, threshold=5.0, above=True)
    )

    labels = iio.imread(labels_path)
    assert result.object_count == 2
    assert set(np.unique(labels)) == {0, 1, 2}


def test_otsu_threshold_segment_uses_global_otsu_threshold(tmp_path: Path) -> None:
    image_path = tmp_path / "input.tif"
    labels_path = tmp_path / "otsu.tif"
    image = np.full((8, 8), 10, dtype=np.uint8)
    image[1:3, 1:3] = 200
    image[5:7, 5:7] = 200
    iio.imwrite(image_path, image)

    result = OtsuThresholdSegment().process_row(
        Arguments(input_image=image_path, labels=labels_path, above=True)
    )

    labels = iio.imread(labels_path)
    assert result.threshold == pytest.approx(float(threshold_otsu(image)))
    assert result.object_count == 2
    assert set(np.unique(labels)) == {0, 1, 2}


def test_local_threshold_segment_uses_sauvola_threshold(tmp_path: Path) -> None:
    image_path = tmp_path / "input.tif"
    labels_path = tmp_path / "local.tif"
    image = np.full((9, 9), 20, dtype=np.uint8)
    image[2:4, 2:4] = 180
    image[5:7, 5:7] = 180
    iio.imwrite(image_path, image)

    result = LocalThresholdSegment().process_row(
        Arguments(
            input_image=image_path,
            labels=labels_path,
            block_size=3,
            k=0.2,
            offset=-20.0,
            above=True,
        )
    )

    labels = iio.imread(labels_path)
    assert result.object_count == 2
    assert set(np.unique(labels)) == {0, 1, 2}


def test_watershed_segment_uses_markers_to_split_foreground(tmp_path: Path) -> None:
    image_path = tmp_path / "input.tif"
    markers_path = tmp_path / "markers.tif"
    labels_path = tmp_path / "labels.tif"

    image = np.zeros((7, 7), dtype=np.uint8)
    image[1:6, 1:6] = 10
    markers = np.zeros((7, 7), dtype=np.uint16)
    markers[2, 2] = 7
    markers[4, 4] = 11
    iio.imwrite(image_path, image)
    iio.imwrite(markers_path, markers)

    result = WatershedSegment().process_row(
        Arguments(
            input_image=image_path,
            markers_image=markers_path,
            labels=labels_path,
            threshold=5.0,
        )
    )

    labels = iio.imread(labels_path)
    assert result.object_count == 2
    assert labels[2, 2] == 7
    assert labels[4, 4] == 11
    assert labels[0, 0] == 0
    assert set(np.unique(labels)) == {0, 7, 11}


def test_distance_watershed_segment_splits_touching_binary_objects(
    tmp_path: Path,
) -> None:
    image_path = tmp_path / "mask.tif"
    labels_path = tmp_path / "distance.tif"
    yy, xx = np.ogrid[:11, :13]
    mask = ((yy - 5) ** 2 + (xx - 4) ** 2 <= 9) | (
        (yy - 5) ** 2 + (xx - 8) ** 2 <= 9
    )
    iio.imwrite(image_path, mask.astype(np.uint8))

    result = DistanceWatershedSegment().process_row(
        Arguments(
            input_image=image_path,
            labels=labels_path,
            threshold=0.5,
            min_distance=3,
        )
    )

    labels = iio.imread(labels_path)
    assert result.object_count == 2
    assert labels[5, 4] != 0
    assert labels[5, 8] != 0
    assert labels[5, 4] != labels[5, 8]


def test_split_touching_objects_splits_each_clumped_label(tmp_path: Path) -> None:
    input_path = tmp_path / "clump.tif"
    output_path = tmp_path / "split.tif"
    yy, xx = np.ogrid[:11, :13]
    labels_in = np.zeros((11, 13), dtype=np.uint16)
    labels_in[
        ((yy - 5) ** 2 + (xx - 4) ** 2 <= 9)
        | ((yy - 5) ** 2 + (xx - 8) ** 2 <= 9)
    ] = 5
    iio.imwrite(input_path, labels_in)

    result = SplitTouchingObjects().process_row(
        Arguments(labels=input_path, output_labels=output_path, min_distance=3)
    )

    labels = iio.imread(output_path)
    assert result.object_count == 2
    assert labels[5, 4] != labels[5, 8]
    assert set(np.unique(labels)) == {0, 1, 2}


def test_filter_labels_removes_by_area_border_and_intensity(tmp_path: Path) -> None:
    input_path = tmp_path / "labels.tif"
    intensity_path = tmp_path / "intensity.tif"
    output_path = tmp_path / "filtered.tif"
    labels = np.zeros((8, 8), dtype=np.uint16)
    labels[1:4, 1:4] = 5
    labels[0:3, 5:7] = 7
    labels[6, 6] = 9
    intensity = np.zeros((8, 8), dtype=np.float32)
    intensity[labels == 5] = 10.0
    intensity[labels == 7] = 10.0
    intensity[labels == 9] = 2.0
    iio.imwrite(input_path, labels)
    iio.imwrite(intensity_path, intensity)

    result = FilterLabels().process_row(
        Arguments(
            labels=input_path,
            output_labels=output_path,
            min_area=4,
            max_area=20,
            remove_border_touching=True,
            intensity_image=intensity_path,
            min_mean_intensity=5.0,
            min_solidity=0.0,
            max_eccentricity=1.0,
        )
    )

    output = iio.imread(output_path)
    assert result.object_count == 1
    assert set(np.unique(output)) == {0, 1}
    assert output[2, 2] == 1
    assert output[1, 5] == 0
    assert output[6, 6] == 0


def test_filter_labels_handles_volumetric_labels_without_2d_shape_filters(
    tmp_path: Path,
) -> None:
    input_path = tmp_path / "labels_3d.tif"
    output_path = tmp_path / "filtered_3d.tif"
    labels = np.zeros((4, 8, 8), dtype=np.uint16)
    labels[1:3, 2:5, 2:5] = 5
    labels[:, 0:2, 0:2] = 7
    iio.imwrite(input_path, labels, photometric="minisblack")

    result = FilterLabels().process_row(
        Arguments(
            labels=input_path,
            output_labels=output_path,
            min_area=8,
            max_area=0,
            remove_border_touching=True,
            intensity_image="",
            min_mean_intensity=0.0,
            min_solidity=0.9,
            max_eccentricity=0.1,
        )
    )

    output = iio.imread(output_path)
    assert result.object_count == 1
    assert set(np.unique(output)) == {0, 1}
    assert output[1, 3, 3] == 1
    assert output[1, 0, 0] == 0


def test_postprocess_labels_removes_small_objects_and_relabels(tmp_path: Path) -> None:
    input_path = tmp_path / "labels.tif"
    output_path = tmp_path / "post.tif"
    labels = np.zeros((8, 8), dtype=np.uint16)
    labels[1:4, 1:4] = 5
    labels[6, 6] = 9
    iio.imwrite(input_path, labels)

    result = PostprocessLabels().process_row(
        Arguments(labels=input_path, output_labels=output_path, min_size=4)
    )

    output = iio.imread(output_path)
    assert result.object_count == 1
    assert set(np.unique(output)) == {0, 1}
    assert output[2, 2] == 1
    assert output[6, 6] == 0


def test_heavy_segmentation_tools_build_graph_without_model_dependencies(
    tmp_path: Path,
) -> None:
    with Workflow(storage_path=tmp_path / "results", use_wetlands=False) as wf:
        Cellpose3()(input_image=tmp_path / "image.tif", name="cellpose")
        StarDistSegmenter()(input_image=tmp_path / "image.tif", name="stardist")

        assert isinstance(wf.nodes["cellpose"].tool, Cellpose3)
        assert isinstance(wf.nodes["stardist"].tool, StarDistSegmenter)
        assert (
            wf.get_environment(Cellpose3.environment).name == Cellpose3.environment.name
        )
        assert (
            wf.get_environment(StarDistSegmenter.environment).name
            == StarDistSegmenter.environment.name
        )


@pytest.mark.model_runtime
@pytest.mark.complete
@pytest.mark.skipif(
    importlib.util.find_spec("cellpose") is None,
    reason="Cellpose3 complete runtime test requires optional cellpose",
)
def test_cellpose3_runtime_segments_tiny_synthetic_image(tmp_path: Path) -> None:
    image_path = tmp_path / "cellpose_input.tif"
    mask_path = tmp_path / "cellpose_mask.tif"
    image = np.zeros((32, 32), dtype=np.float32)
    image[10:22, 10:22] = 1.0
    iio.imwrite(image_path, image)

    result = Cellpose3().process_row(
        Arguments(
            input_image=image_path,
            mask=mask_path,
            diameter=12.0,
            model_type="cyto3",
            channel=0,
            nuclear_channel=0,
            flow_threshold=0.4,
            cellprob_threshold=-6.0,
        )
    )

    labels = iio.imread(mask_path)
    assert labels.shape == image.shape
    assert result.cell_count == int(labels.max())


@pytest.mark.model_runtime
@pytest.mark.complete
@pytest.mark.skipif(
    importlib.util.find_spec("stardist") is None
    or importlib.util.find_spec("csbdeep") is None,
    reason="StarDistSegmenter complete runtime test requires optional stardist/csbdeep",
)
def test_stardist_runtime_segments_tiny_synthetic_image(tmp_path: Path) -> None:
    image_path = tmp_path / "stardist_input.tif"
    mask_path = tmp_path / "stardist_mask.tif"
    yy, xx = np.ogrid[:48, :48]
    image = (((yy - 24) ** 2 + (xx - 24) ** 2) <= 64).astype(np.float32)
    iio.imwrite(image_path, image)

    result = StarDistSegmenter().process_row(
        Arguments(
            input_image=image_path,
            mask=mask_path,
            model_name="2D_versatile_fluo",
            channel=0,
            prob_thresh=0.1,
            nms_thresh=0.4,
            normalize_low=1.0,
            normalize_high=99.8,
        )
    )

    labels = iio.imread(mask_path)
    assert labels.shape == image.shape
    assert result.object_count == int(labels.max())
