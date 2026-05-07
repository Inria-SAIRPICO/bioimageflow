"""Summarize spot assignments per label."""

from pathlib import Path
from typing import Annotated, Any
import csv

from bioimageflow_core import (
    Arguments,
    Category,
    Connectable,
    GENERAL_ENV,
    GUIMeta,
    IOModel,
    ProcessingTool,
    Template,
)


class SpotSummary(ProcessingTool):
    """Aggregate assigned puncta counts and intensities by label."""

    display_name = "Spot Summary"
    documentation = "Compute per-label spot count and intensity summaries."
    category = Category.MEASUREMENT
    tags = ["spots", "summary", "puncta"]
    environment = GENERAL_ENV

    class Inputs(IOModel):
        assigned_spots_csv: Annotated[
            Path,
            GUIMeta(
                display_name="Assigned spots CSV",
                description="Output from AssignSpotsToLabels.",
                connectable=Connectable.BY_DEFAULT,
            ),
        ]

    class Outputs(IOModel):
        summary_csv: Annotated[Path, GUIMeta(display_name="Spot summary")] = Template(
            "{assigned_spots_csv.stem}_summary.csv"
        )
        label_count: Annotated[int, GUIMeta(display_name="Label count")]

    def process_row(self, arguments: Arguments, *, context: Any = None) -> Any:
        with Path(arguments.assigned_spots_csv).open(newline="") as handle:
            assigned = list(csv.DictReader(handle))
        groups: dict[int, list[float]] = {}
        for row in assigned:
            label = int(float(row["label"]))
            if label <= 0:
                continue
            groups.setdefault(label, []).append(float(row["intensity"]))
        summary = [
            {
                "label": label,
                "spot_count": len(values),
                "mean_intensity": sum(values) / len(values),
                "total_intensity": sum(values),
            }
            for label, values in sorted(groups.items())
        ]

        output = Path(getattr(arguments, "summary_csv", getattr(arguments, "output_csv", "")))
        output.parent.mkdir(parents=True, exist_ok=True)
        fieldnames = ["label", "spot_count", "mean_intensity", "total_intensity"]
        with output.open("w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(summary)
        return self.Outputs(summary_csv=output, label_count=len(summary))
