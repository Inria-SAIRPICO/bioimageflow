from __future__ import annotations

import json
from pathlib import Path

from bioimageflow import Workflow


def test_io_and_measurement_packages_are_not_exported_as_custom_sources(
    tmp_path: Path,
) -> None:
    from bioimageflow_io_tools import ReadImage
    from bioimageflow_measurement_tools import CountLabels

    with Workflow(storage_path=tmp_path / "results") as wf:
        read = ReadImage()(input_image=tmp_path / "image.tif", name="read_image")
        CountLabels()(label_image=read["output_image"], name="count_labels")
        wf.export(tmp_path / "workflow.json")

    data = json.loads((tmp_path / "workflow.json").read_text())
    assert "custom_tool_modules" not in data
    assert {node["tool_module"] for node in data["nodes"]} == {
        "bioimageflow_io_tools.image_io",
        "bioimageflow_measurement_tools.measurements",
    }


def test_all_tool_packages_are_not_workflow_custom_classes() -> None:
    from bioimageflow.workflow import _is_workflow_custom_class
    from bioimageflow_io_tools import ReadImage
    from bioimageflow_measurement_tools import CountLabels
    from bioimageflow_restoration_tools import RestoreImage
    from bioimageflow_sairpico_tools import MedianDenoising
    from bioimageflow_segmentation_tools import ThresholdSegment
    from bioimageflow_spot_tools import DetectSpots
    from bioimageflow_tracking_tools import LinkObjects

    for tool_cls in [
        ReadImage,
        CountLabels,
        RestoreImage,
        MedianDenoising,
        ThresholdSegment,
        DetectSpots,
        LinkObjects,
    ]:
        assert _is_workflow_custom_class(tool_cls) is False
