"""Reusable marker-spot workflow used by the FISH analysis example."""

from pathlib import Path
from typing import Annotated

from bioimageflow import Workflow
from bioimageflow_common_tools import ConnectedComponents, ExtractChannel, LabelOverlaps
from bioimageflow_core import Connectable, GUIMeta, ImageSpec, Semantic
from bioimageflow_spot_tools import AtlasSpotDetection


MarkerImage = Annotated[
    Path,
    ImageSpec(semantics={Semantic.INTENSITY}),
    GUIMeta(
        display_name="Input image",
        description="Multi-channel FISH image.",
        connectable=Connectable.BY_DEFAULT,
    ),
]
NucleiLabels = Annotated[
    Path,
    ImageSpec(semantics={Semantic.LABEL}),
    GUIMeta(
        display_name="Nuclei labels",
        description="Reference nuclei label image.",
        connectable=Connectable.BY_DEFAULT,
    ),
]


def build_workflow() -> Workflow:
    """Return a fresh marker extraction, detection, and overlap workflow."""
    workflow = Workflow(name="marker_spot_analysis", display_name="Marker Spot Analysis")
    with workflow:
        input_image = workflow.input("input_image", MarkerImage, id="input-image")
        nuclei_labels = workflow.input("nuclei_labels", NucleiLabels, id="input-nuclei-labels")
        channel = workflow.input("channel", int, default=0, id="input-channel")
        gaussian_std = workflow.input("gaussian_std", int, default=60, id="input-gaussian-std")
        p_value = workflow.input("p_value", float, default=0.001, id="input-p-value")

        marker_channel = ExtractChannel()(
            input_image=input_image,
            channel=channel,
            name="extract_marker_channel",
        )
        spot_mask = AtlasSpotDetection()(
            input_image=marker_channel["output_image"],
            gaussian_std=gaussian_std,
            p_value=p_value,
            name="detect_marker_spots",
        )
        spot_labels = ConnectedComponents()(
            input_image=spot_mask["output_image"],
            name="label_marker_spots",
        )
        overlaps = LabelOverlaps()(
            label_image=spot_labels["output_image"],
            reference_image=nuclei_labels,
            name="measure_nucleus_overlaps",
        )
        workflow.output("reference_label", overlaps["reference_label"], id="output-reference-label")
        workflow.output("spot_label", overlaps["spot_label"], id="output-spot-label")
        workflow.output("overlap_count", overlaps["overlap_count"], id="output-overlap-count")
    return workflow
