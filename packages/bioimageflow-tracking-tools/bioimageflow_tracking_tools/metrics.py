"""Track-level metrics."""

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


class TrackMetrics(ProcessingTool):
    """Compute track length, displacement, and mean speed summaries."""

    display_name = "Track Metrics"
    documentation = "Compute basic per-track metrics from linked object tables."
    category = Category.MEASUREMENT
    tags = ["tracking", "metrics"]
    environment = GENERAL_ENV

    class Inputs(IOModel):
        tracks_csv: Annotated[
            Path,
            GUIMeta(
                display_name="Tracks CSV",
                description="Track table from LinkObjects.",
                connectable=Connectable.BY_DEFAULT,
            ),
        ]

    class Outputs(IOModel):
        metrics_csv: Annotated[Path, GUIMeta(display_name="Track metrics")] = Template(
            "{tracks_csv.stem}_metrics.csv"
        )
        track_count: Annotated[int, GUIMeta(display_name="Track count")]
        mean_track_length: Annotated[float, GUIMeta(display_name="Mean track length")]

    def process_row(self, arguments: Arguments, *, context: Any = None) -> Any:
        import numpy as np
        with Path(arguments.tracks_csv).open(newline="") as handle:
            tracks = list(csv.DictReader(handle))
        rows = []
        track_ids = sorted({int(float(row["track_id"])) for row in tracks})
        for track_id in track_ids:
            group = sorted(
                (row for row in tracks if int(float(row["track_id"])) == track_id),
                key=lambda row: int(float(row["frame"])),
            )
            first = group[0]
            last = group[-1]
            displacement = float(
                np.hypot(float(last["y"]) - float(first["y"]), float(last["x"]) - float(first["x"]))
            )
            duration = max(1, int(float(last["frame"])) - int(float(first["frame"])))
            rows.append(
                {
                    "track_id": track_id,
                    "track_length": int(len(group)),
                    "start_frame": int(float(first["frame"])),
                    "end_frame": int(float(last["frame"])),
                    "displacement": displacement,
                    "mean_speed": displacement / duration,
                    "mean_area": sum(float(row["area"]) for row in group) / len(group),
                }
            )
        output = Path(arguments.metrics_csv)
        output.parent.mkdir(parents=True, exist_ok=True)
        fieldnames = [
            "track_id",
            "track_length",
            "start_frame",
            "end_frame",
            "displacement",
            "mean_speed",
            "mean_area",
        ]
        with output.open("w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)
        mean_length = (
            sum(float(row["track_length"]) for row in rows) / len(rows) if rows else 0.0
        )
        return self.Outputs(
            metrics_csv=output,
            track_count=len(rows),
            mean_track_length=mean_length,
        )
