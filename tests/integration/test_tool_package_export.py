from __future__ import annotations

import json
from pathlib import Path

from bioimageflow import Workflow


def test_io_and_measurement_packages_are_not_exported_as_custom_sources(
    tmp_path: Path,
) -> None:
    from bioimageflow_io_tools import ConvertImageFormat
    from bioimageflow_measurement_tools import CountLabels

    with Workflow(engine="direct", storage_path=tmp_path / "results") as wf:
        converted = ConvertImageFormat()(
            input_image=tmp_path / "image.tif",
            name="convert_image_format",
        )
        CountLabels()(label_image=converted["output_image"], name="count_labels")
        wf.export(tmp_path / "workflow.json")

    data = json.loads((tmp_path / "workflow.json").read_text())
    assert "custom_sources" not in data
    assert {node["tool_module"] for node in data["nodes"]} == {
        "bioimageflow_io_tools.writers",
        "bioimageflow_measurement_tools.processing_tools",
    }


def test_all_tool_packages_are_not_workflow_custom_classes() -> None:
    from bioimageflow.workflow import _is_workflow_custom_class
    from bioimageflow_io_tools import ConvertImageFormat
    from bioimageflow_measurement_tools import CountLabels
    from bioimageflow_restoration_tools import TotalVariationDenoise
    from bioimageflow_sairpico_tools import MedianDenoising
    from bioimageflow_segmentation_tools import ThresholdSegment
    from bioimageflow_spot_tools import DetectSpots
    from bioimageflow_tracking_tools import NearestNeighborLink

    for tool_cls in [
        ConvertImageFormat,
        CountLabels,
        TotalVariationDenoise,
        MedianDenoising,
        ThresholdSegment,
        DetectSpots,
        NearestNeighborLink,
    ]:
        assert _is_workflow_custom_class(tool_cls) is False
