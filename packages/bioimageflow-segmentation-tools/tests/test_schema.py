"""Schema tests for the segmentation tool package."""

from __future__ import annotations

import json

import pytest

from bioimageflow.validation import serialize_input_schema, serialize_output_schema
from bioimageflow_core import BaseTool
from bioimageflow_segmentation_tools import (
    Cellpose3,
    CellposeSAM,
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
SEGMENTATION_TOOLS = [
    Cellpose3,
    CellposeSAM,
    StarDistSegmenter,
    ThresholdSegment,
    OtsuThresholdSegment,
    LocalThresholdSegment,
    WatershedSegment,
    DistanceWatershedSegment,
    SplitTouchingObjects,
    FilterLabels,
    PostprocessLabels,
]

pytestmark = pytest.mark.package_tools


def test_segmentation_package_all_exports_only_public_tools() -> None:
    import bioimageflow_segmentation_tools as segmentation

    assert sorted(segmentation.__all__) == [
        "Cellpose3",
        "CellposeSAM",
        "DistanceWatershedSegment",
        "FilterLabels",
        "LocalThresholdSegment",
        "OtsuThresholdSegment",
        "PostprocessLabels",
        "SplitTouchingObjects",
        "StarDistSegmenter",
        "ThresholdSegment",
        "WatershedSegment",
    ]
    assert "classical" not in segmentation.__all__
    assert all(issubclass(getattr(segmentation, name), BaseTool) for name in segmentation.__all__)


@pytest.mark.parametrize("tool_cls", SEGMENTATION_TOOLS, ids=lambda c: c.__name__)
def test_segmentation_tool_schemas_are_json_serializable(tool_cls: type) -> None:
    inputs = serialize_input_schema(tool_cls)
    outputs = serialize_output_schema(tool_cls)
    json.dumps(inputs)
    json.dumps(outputs)

    assert set(inputs) == set(tool_cls.Inputs._get_all_annotations())
    assert outputs


def test_image_schema_marks_label_outputs_as_image_files() -> None:
    threshold_outputs = serialize_output_schema(ThresholdSegment)
    otsu_outputs = serialize_output_schema(OtsuThresholdSegment)
    local_outputs = serialize_output_schema(LocalThresholdSegment)
    watershed_outputs = serialize_output_schema(WatershedSegment)
    distance_outputs = serialize_output_schema(DistanceWatershedSegment)
    split_outputs = serialize_output_schema(SplitTouchingObjects)
    filter_outputs = serialize_output_schema(FilterLabels)
    postprocess_outputs = serialize_output_schema(PostprocessLabels)

    assert threshold_outputs["labels"]["type"] == "ImageFile"
    assert threshold_outputs["labels"]["image_spec"]["semantics"] == ["label"]
    assert otsu_outputs["labels"]["type"] == "ImageFile"
    assert local_outputs["labels"]["type"] == "ImageFile"
    assert watershed_outputs["labels"]["type"] == "ImageFile"
    assert distance_outputs["labels"]["type"] == "ImageFile"
    assert split_outputs["output_labels"]["type"] == "ImageFile"
    assert filter_outputs["output_labels"]["type"] == "ImageFile"
    assert postprocess_outputs["output_labels"]["type"] == "ImageFile"


def test_watershed_marker_schema_is_image_file_with_empty_default() -> None:
    inputs = serialize_input_schema(WatershedSegment)

    assert inputs["markers_image"]["type"] == "ImageFile"
    assert inputs["markers_image"]["default"] == ""
    assert inputs["markers_image"]["required"] is False
    assert inputs["markers_image"]["image_spec"]["semantics"] == ["label"]
