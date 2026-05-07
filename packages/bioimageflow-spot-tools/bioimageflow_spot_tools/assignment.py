"""Assign detected spots to label images."""

from pathlib import Path
from typing import Annotated, Any
import csv

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
    Template,
)


class AssignSpotsToLabels(ProcessingTool):
    """Assign each spot coordinate to the label value at the same pixel."""

    display_name = "Assign Spots to Labels"
    documentation = "Assign detected spot coordinates to nuclei or cell label images."
    category = Category.MEASUREMENT
    tags = ["spots", "labels", "assignment"]
    environment = GENERAL_ENV

    class Inputs(IOModel):
        spots_csv: Annotated[
            Path,
            GUIMeta(
                display_name="Spots CSV",
                description="Spot table with y and x columns.",
                connectable=Connectable.BY_DEFAULT,
            ),
        ]
        label_image: Annotated[
            Path,
            ImageSpec(semantics={Semantic.LABEL}, layouts={Layout.PLANAR}),
            GUIMeta(
                display_name="Label image",
                description="2D label image used for spot assignment.",
                connectable=Connectable.BY_DEFAULT,
            ),
        ]

    class Outputs(IOModel):
        assigned_spots_csv: Annotated[Path, GUIMeta(display_name="Assigned spots")] = (
            Template("{spots_csv.stem}_assigned.csv")
        )
        assigned_count: Annotated[int, GUIMeta(display_name="Assigned spot count")]

    def process_row(self, arguments: Arguments, *, context: Any = None) -> Any:
        import imageio.v3 as iio

        labels = iio.imread(arguments.label_image)
        with Path(arguments.spots_csv).open(newline="") as handle:
            rows = list(csv.DictReader(handle))
        assigned_count = 0
        for row in rows:
            y = int(float(row["y"]))
            x = int(float(row["x"]))
            label = int(labels[y, x]) if 0 <= y < labels.shape[0] and 0 <= x < labels.shape[1] else 0
            row["label"] = label
            if label > 0:
                assigned_count += 1

        output = Path(
            getattr(arguments, "assigned_spots_csv", getattr(arguments, "output_csv", ""))
        )
        output.parent.mkdir(parents=True, exist_ok=True)
        fieldnames = ["spot_id", "y", "x", "intensity", "score", "label"]
        with output.open("w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)
        return self.Outputs(
            assigned_spots_csv=output,
            assigned_count=assigned_count,
        )
