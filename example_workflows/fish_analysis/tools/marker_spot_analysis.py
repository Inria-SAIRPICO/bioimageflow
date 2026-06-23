"""Canonical marker spot sub-workflow for FISH analysis."""

from pathlib import Path
from typing import Annotated

from bioimageflow.sub_workflow import SubWorkflow
from bioimageflow_common_tools import ConnectedComponents, ExtractChannel, LabelOverlaps
from bioimageflow_core import Connectable, GUIMeta, IOModel, ImageSpec, Semantic
from bioimageflow_spot_tools import AtlasSpotDetection


class MarkerSpotAnalysis(SubWorkflow):
    """Extract one marker channel, detect ATLAS spots, and overlap spots with nuclei."""

    display_name = "Marker Spot Analysis"

    class Inputs(IOModel):
        input_image: Annotated[
            Path,
            ImageSpec(semantics={Semantic.INTENSITY}),
            GUIMeta(
                display_name="Input image",
                description="Multi-channel FISH image.",
                connectable=Connectable.BY_DEFAULT,
            ),
        ]
        nuclei_labels: Annotated[
            Path,
            ImageSpec(semantics={Semantic.LABEL}),
            GUIMeta(
                display_name="Nuclei labels",
                description="Reference nuclei label image.",
                connectable=Connectable.BY_DEFAULT,
            ),
        ]
        marker_name: Annotated[
            str,
            GUIMeta(display_name="Marker name", description="Marker label used for display."),
        ] = "marker"
        channel: Annotated[
            int,
            GUIMeta(display_name="Channel", description="Marker channel index.", min=0, step=1),
        ] = 0
        gaussian_std: Annotated[
            int,
            GUIMeta(display_name="Gaussian std", min=1, max=200, step=1),
        ] = 60
        p_value: Annotated[
            float,
            GUIMeta(display_name="P-value", min=0.0, max=1.0, step=0.000001),
        ] = 0.001

    class Outputs(IOModel):
        reference_label: Annotated[int, GUIMeta(display_name="Reference label")]
        spot_label: Annotated[int, GUIMeta(display_name="Spot label")]
        overlap_count: Annotated[int, GUIMeta(display_name="Overlap count")]

    def build(self, inputs):  # type: ignore[override]
        marker_channel = ExtractChannel()(
            input_image=inputs.input_image,
            channel=inputs.channel,
        )
        spot_mask = AtlasSpotDetection()(
            input_image=marker_channel["output_image"],
            gaussian_std=inputs.gaussian_std,
            p_value=inputs.p_value,
        )
        spot_labels = ConnectedComponents()(input_image=spot_mask["output_image"])
        overlaps = LabelOverlaps()(
            label_image=spot_labels["output_image"],
            reference_image=inputs.nuclei_labels,
        )
        return {
            "reference_label": overlaps["reference_label"],
            "spot_label": overlaps["spot_label"],
            "overlap_count": overlaps["overlap_count"],
        }
