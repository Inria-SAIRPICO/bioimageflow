"""LabelOverlaps — compute spatial overlap between two label images."""

from pathlib import Path
from typing import Annotated, Any

from bioimageflow_core import (
    Arguments,
    Category,
    Connectable,
    GENERAL_ENV,
    GUIMeta,
    ImageSpec,
    IOModel,
    Layout,
    ProcessingTool,
    RowConsumption,
    Semantic,
)


class LabelOverlaps(ProcessingTool):
    """Compute spatial overlap between a label image and a reference label image.

    For each pixel, records which label from image 1 overlaps with which
    label from image 2. Outputs a DataFrame with columns:
    reference_label, spot_label, overlap_count.
    """
    row_consumption = RowConsumption.MAPPED
    display_name = "Label Overlaps"
    documentation = (
        "Compute the spatial overlap between two labeled images. "
        "Outputs a table of (reference_label, spot_label, overlap_count) tuples."
    )
    category = Category.MEASUREMENT
    tags = ["measurement", "spatial correlation"]
    environment = GENERAL_ENV

    class Inputs(IOModel):
        label_image: Annotated[
            Path,
            ImageSpec(
                semantics={Semantic.LABEL},
                layouts={Layout.PLANAR},
            ),
            GUIMeta(
                display_name="Label image",
                description=(
                    "Label image whose regions will be matched against the reference."
                ),
                connectable=Connectable.BY_DEFAULT,
            ),
        ]
        reference_image: Annotated[
            Path,
            ImageSpec(
                semantics={Semantic.LABEL},
                layouts={Layout.PLANAR},
            ),
            GUIMeta(
                display_name="Reference label image",
                description=(
                    "Reference label image. Each pixel of the label image is "
                    "paired with the reference label at the same location."
                ),
                connectable=Connectable.BY_DEFAULT,
            ),
        ]

    class Outputs(IOModel):
        reference_label: Annotated[int, GUIMeta(
            display_name="Reference label",
            description="Label value from the reference image.",
        )]
        spot_label: Annotated[int, GUIMeta(
            display_name="Spot label",
            description="Label value from the label image.",
        )]
        overlap_count: Annotated[int, GUIMeta(
            display_name="Overlap count",
            description="Number of pixels where the two labels overlap.",
        )]

    def process_row(self, arguments: Arguments, *, context: Any = None) -> Any:
        import numpy as np
        import imageio.v3 as iio

        print("Computing label overlaps...")
        labels = iio.imread(str(arguments.label_image))
        reference = iio.imread(str(arguments.reference_image))
        labels = _validated_label_image(labels, "Label image", np)
        reference = _validated_label_image(reference, "Reference label image", np)
        if labels.shape != reference.shape:
            raise ValueError(
                "Label image and reference label image must have the same shape; "
                f"got {labels.shape} and {reference.shape}."
            )

        # Build overlap table: for each pixel, record (reference_label, spot_label)
        mask = (labels > 0) | (reference > 0)
        ref_vals = reference[mask]
        lbl_vals = labels[mask]
        if ref_vals.size == 0:
            print("Label overlaps: 0 pairs")
            return []

        # Count co-occurrences
        pairs, counts = np.unique(
            np.stack([ref_vals, lbl_vals], axis=1), axis=0, return_counts=True
        )

        print(f"Label overlaps: {len(pairs)} pairs")

        return [
            self.Outputs(
                reference_label=int(ref_lbl),
                spot_label=int(spot_lbl),
                overlap_count=int(count),
            )
            for (ref_lbl, spot_lbl), count in zip(pairs, counts)
        ]


def _validated_label_image(image: Any, name: str, np: Any) -> Any:
    if image.ndim != 2:
        raise ValueError(f"{name} must be a 2D label image; got shape {image.shape}.")
    if image.dtype.kind not in "biuf":
        raise ValueError(f"{name} must contain numeric labels.")
    if not np.isfinite(image).all():
        raise ValueError(f"{name} must contain only finite labels.")
    if (image < 0).any():
        raise ValueError(f"{name} must contain only non-negative labels.")
    if image.dtype.kind == "f" and not np.equal(image, np.floor(image)).all():
        raise ValueError(f"{name} must contain only integer-valued labels.")
    max_label = image.max(initial=0)
    if max_label > np.iinfo(np.uint64).max:
        raise ValueError(f"{name} contains labels larger than uint64 supports.")
    return image.astype(np.uint64, copy=False)
