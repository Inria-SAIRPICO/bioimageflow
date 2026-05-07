"""Execution and graph-construction tests for segmentation package tools."""

from __future__ import annotations

from pathlib import Path

import imageio.v3 as iio
import numpy as np

from bioimageflow import Workflow
from bioimageflow_core import Arguments
from bioimageflow_segmentation_tools import (
    Cellpose3,
    PostprocessLabels,
    StarDistSegmenter,
    ThresholdSegment,
    WatershedSegment,
)


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
