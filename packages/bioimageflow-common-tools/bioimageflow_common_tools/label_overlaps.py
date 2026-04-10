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
    Semantic,
)


class LabelOverlaps(ProcessingTool):
    """Compute spatial overlap between a label image and a reference label image.

    For each pixel, records which label from image 1 overlaps with which
    label from image 2. Outputs a CSV table with columns:
    reference_label, spot_label, overlap_count.
    """
    display_name = "Label Overlaps"
    documentation = (
        "Compute the spatial overlap between two labeled images. "
        "Outputs a CSV table of (reference_label, spot_label, overlap_count) tuples."
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
            GUIMeta(connectable=Connectable.BY_DEFAULT),
        ]
        reference_image: Annotated[
            Path,
            ImageSpec(
                semantics={Semantic.LABEL},
                layouts={Layout.PLANAR},
            ),
            GUIMeta(connectable=Connectable.BY_DEFAULT),
        ]

    class Outputs(IOModel):
        overlaps: Path = Path("{label_image.stem}_overlaps.csv")

    def process_row(self, arguments: Arguments) -> Any:
        import numpy as np
        import imageio.v3 as iio

        print("Computing label overlaps...")
        labels = iio.imread(str(arguments.label_image))
        reference = iio.imread(str(arguments.reference_image))

        # Build overlap table: for each pixel, record (reference_label, spot_label)
        mask = (labels > 0) | (reference > 0)
        ref_vals = reference[mask]
        lbl_vals = labels[mask]

        # Count co-occurrences
        pairs, counts = np.unique(
            np.stack([ref_vals, lbl_vals], axis=1), axis=0, return_counts=True
        )

        output_path = Path(arguments.overlaps)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        lines = ["reference_label,spot_label,overlap_count"]
        for (ref_lbl, spot_lbl), count in zip(pairs, counts):
            lines.append(f"{int(ref_lbl)},{int(spot_lbl)},{int(count)}")
        output_path.write_text("\n".join(lines) + "\n")
        print(f"Label overlaps: {len(pairs)} pairs")

        return self.Outputs(overlaps=output_path)
