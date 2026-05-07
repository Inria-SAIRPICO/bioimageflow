"""Schema tests for the segmentation tool package."""

from __future__ import annotations

import json

import pytest

from bioimageflow.validation import serialize_input_schema, serialize_output_schema
from bioimageflow_segmentation_tools import (
    Cellpose3,
    PostprocessLabels,
    StarDistSegmenter,
    ThresholdSegment,
    WatershedSegment,
)
from bioimageflow_segmentation_tools.cellpose_v3 import cellpose_v3_env
from bioimageflow_segmentation_tools.classical import classical_segmentation_env
from bioimageflow_segmentation_tools.stardist_segmenter import stardist_env


SEGMENTATION_TOOLS = [
    Cellpose3,
    StarDistSegmenter,
    ThresholdSegment,
    WatershedSegment,
    PostprocessLabels,
]


@pytest.mark.parametrize("tool_cls", SEGMENTATION_TOOLS, ids=lambda c: c.__name__)
def test_segmentation_tool_schemas_are_json_serializable(tool_cls: type) -> None:
    inputs = serialize_input_schema(tool_cls)
    outputs = serialize_output_schema(tool_cls)
    json.dumps(inputs)
    json.dumps(outputs)

    assert set(inputs) == set(tool_cls.Inputs._get_all_annotations())
    assert outputs


def test_heavy_tool_environments_are_isolated() -> None:
    assert Cellpose3.environment is cellpose_v3_env
    assert "cellpose==3.1.1.1" in cellpose_v3_env.dependencies["pip"]
    assert "tensorflow" not in cellpose_v3_env.dependencies["pip"]

    assert StarDistSegmenter.environment is stardist_env
    assert "stardist==0.9.2" in stardist_env.dependencies["pip"]
    assert "cellpose==3.1.1.1" not in stardist_env.dependencies["pip"]


def test_classical_tools_share_lightweight_environment() -> None:
    assert ThresholdSegment.environment is classical_segmentation_env
    assert WatershedSegment.environment is classical_segmentation_env
    assert PostprocessLabels.environment is classical_segmentation_env
    assert classical_segmentation_env.dependencies["pip"] == [
        "imageio",
        "numpy",
        "scikit-image",
        "tifffile",
    ]


def test_image_schema_marks_label_outputs_as_image_files() -> None:
    threshold_outputs = serialize_output_schema(ThresholdSegment)
    watershed_outputs = serialize_output_schema(WatershedSegment)
    postprocess_outputs = serialize_output_schema(PostprocessLabels)

    assert threshold_outputs["labels"]["type"] == "ImageFile"
    assert threshold_outputs["labels"]["image_spec"]["semantics"] == ["label"]
    assert watershed_outputs["labels"]["type"] == "ImageFile"
    assert postprocess_outputs["output_labels"]["type"] == "ImageFile"


def test_watershed_marker_schema_is_image_file_with_empty_default() -> None:
    inputs = serialize_input_schema(WatershedSegment)

    assert inputs["markers_image"]["type"] == "ImageFile"
    assert inputs["markers_image"]["default"] == ""
    assert inputs["markers_image"]["required"] is False
    assert inputs["markers_image"]["image_spec"]["semantics"] == ["label"]


def test_common_package_no_longer_exposes_canonical_segmentation_wrappers() -> None:
    import importlib.util

    import bioimageflow_common_tools as common_tools

    assert not hasattr(common_tools, "Cellpose3")
    assert not hasattr(common_tools, "CellposeSAM")
    assert not hasattr(common_tools, "StarDistSegmenter")
    assert importlib.util.find_spec("bioimageflow_common_tools.cellpose_v3") is None
    assert importlib.util.find_spec("bioimageflow_common_tools.cellpose_sam") is None
    assert (
        importlib.util.find_spec("bioimageflow_common_tools.stardist_segmenter") is None
    )
