"""
Reusable sub-workflows for FISH image analysis.

Demonstrates how recurring multi-step patterns can be packaged as
SubWorkflows and composed — including nesting (sub-sub-workflows).

Sub-workflows defined here:

  SpotDetection
  ~~~~~~~~~~~~~
  Extract a channel → detect spots with Atlas → label connected components.
  Used twice in the FISH pipeline (once for FOLS2, once for CSF1R).

  SpotAnalysis  (nested — contains SpotDetection)
  ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
  Runs SpotDetection on one channel, then computes label overlaps against
  a reference segmentation (e.g. nuclei). This demonstrates sub-sub-workflows.
"""

from pathlib import Path
from typing import Annotated

from bioimageflow_core import Connectable, GUIMeta, IOModel, ImageSpec, Semantic
from bioimageflow.sub_workflow import SubWorkflow

from bioimageflow_common_tools import (
    ExtractChannel,
    ConnectedComponents,
    LabelOverlaps,
)
from bioimageflow_common_tools.atlas import Atlas


# ---------------------------------------------------------------------------
# SpotDetection: Extract channel → Atlas → Connected components
# ---------------------------------------------------------------------------

class SpotDetection(SubWorkflow):
    """Detect and label spots in a single channel of a multi-channel image.

    Pipeline::

        ExtractChannel → Atlas → ConnectedComponents

    Inputs
    ------
    input_image : Path
        Multi-channel image (CYX or CZYX).
    channel : int
        Channel index to extract (0-based).
    gaussian_std : int
        Atlas spot size parameter.
    p_value : float
        Atlas sensitivity parameter.

    Outputs
    -------
    labeled_spots : Path
        Label image where each spot has a unique integer ID.
    num_spots : int
        Number of detected spots.
    """

    display_name = "Spot Detection"

    class Inputs(IOModel):
        input_image: Annotated[Path, ImageSpec(semantics={Semantic.INTENSITY}), GUIMeta(
            display_name="Input image",
            description="Multi-channel intensity image in which spots are detected.",
            connectable=Connectable.BY_DEFAULT,
        )]
        channel: Annotated[int, GUIMeta(
            display_name="Channel",
            description="Channel index used for spot detection (0-based).",
            min=0, step=1,
        )] = 0
        gaussian_std: Annotated[int, GUIMeta(
            display_name="Gaussian std",
            description="Atlas spot-size parameter (Gaussian kernel std, in pixels).",
            min=0, max=200, step=1,
        )] = 60
        p_value: Annotated[float, GUIMeta(
            display_name="P-value",
            description="Atlas detection significance threshold. Lower values yield fewer, more confident spots.",
            min=0.0, max=1.0, step=0.000001,
        )] = 0.001

    class Outputs(IOModel):
        labeled_spots: Annotated[Path, ImageSpec(semantics={Semantic.LABEL}), GUIMeta(
            display_name="Labeled spots",
            description="Label image; each detected spot has a unique integer ID.",
        )]
        num_spots: Annotated[int, GUIMeta(
            display_name="Spot count",
            description="Number of detected spots.",
        )]

    def build(self, inputs):  # type: ignore[override]
        extract = ExtractChannel()
        atlas = Atlas()
        cc = ConnectedComponents()

        channel = extract(
            input_image=inputs.input_image,
            channel=inputs.channel,
        )
        spots = atlas(
            input_image=channel["output_image"],
            gaussian_std=inputs.gaussian_std,
            p_value=inputs.p_value,
        )
        labels = cc(input_image=spots["output_image"])

        return {
            "labeled_spots": labels["output_image"],
            "num_spots": labels["num_labels"],
        }


# ---------------------------------------------------------------------------
# SpotAnalysis: SpotDetection → LabelOverlaps  (nested sub-workflow)
# ---------------------------------------------------------------------------

class SpotAnalysis(SubWorkflow):
    """Detect spots in a channel and compute their overlap with a reference.

    This is a nested sub-workflow: it uses SpotDetection internally,
    then adds a LabelOverlaps step to measure spatial correlation with
    a reference segmentation (typically nuclei).

    Pipeline::

        SpotDetection (sub-workflow) → LabelOverlaps

    Inputs
    ------
    input_image : Path
        Multi-channel image.
    reference_image : Path
        Reference label image (e.g. segmented nuclei).
    channel : int
        Channel index for spot detection.
    gaussian_std : int
        Atlas spot size parameter.
    p_value : float
        Atlas sensitivity parameter.

    Outputs
    -------
    reference_label : int
        Label value from the reference image.
    spot_label : int
        Label value from the detected spot image.
    overlap_count : int
        Number of pixels where the two labels overlap.
    """

    display_name = "Spot Analysis"

    class Inputs(IOModel):
        input_image: Annotated[Path, ImageSpec(semantics={Semantic.INTENSITY}), GUIMeta(
            display_name="Input image",
            description="Multi-channel intensity image to detect spots in.",
            connectable=Connectable.BY_DEFAULT,
        )]
        reference_image: Annotated[Path, ImageSpec(semantics={Semantic.LABEL}), GUIMeta(
            display_name="Reference label image",
            description="Reference segmentation (e.g. nuclei) used to measure spatial correlation with the detected spots.",
            connectable=Connectable.BY_DEFAULT,
        )]
        channel: Annotated[int, GUIMeta(
            display_name="Channel",
            description="Channel index used for spot detection (0-based).",
            min=0, step=1,
        )] = 0
        gaussian_std: Annotated[int, GUIMeta(
            display_name="Gaussian std",
            description="Atlas spot-size parameter (Gaussian kernel std, in pixels).",
            min=0, max=200, step=1,
        )] = 60
        p_value: Annotated[float, GUIMeta(
            display_name="P-value",
            description="Atlas detection significance threshold. Lower values yield fewer, more confident spots.",
            min=0.0, max=1.0, step=0.000001,
        )] = 0.001

    class Outputs(IOModel):
        reference_label: Annotated[int, GUIMeta(
            display_name="Reference label",
            description="Label value from the reference image.",
        )]
        spot_label: Annotated[int, GUIMeta(
            display_name="Spot label",
            description="Label value from the detected spot image.",
        )]
        overlap_count: Annotated[int, GUIMeta(
            display_name="Overlap count",
            description="Number of pixels where the two labels overlap.",
        )]

    def build(self, inputs):  # type: ignore[override]
        detect = SpotDetection()
        overlap = LabelOverlaps()

        spots = detect(
            input_image=inputs.input_image,
            channel=inputs.channel,
            gaussian_std=inputs.gaussian_std,
            p_value=inputs.p_value,
        )
        overlaps = overlap(
            label_image=spots["labeled_spots"],
            reference_image=inputs.reference_image,
        )

        return {
            "reference_label": overlaps["reference_label"],
            "spot_label": overlaps["spot_label"],
            "overlap_count": overlaps["overlap_count"],
        }
