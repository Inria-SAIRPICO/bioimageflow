"""Greedy nearest-neighbor object linking."""

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


class LinkObjects(ProcessingTool):
    """Link objects frame-to-frame with a lightweight nearest-neighbor method."""

    display_name = "Link Objects"
    documentation = (
        "Greedy nearest-neighbor frame linking. btrack/LapTrack integrations are "
        "optional and reserved for heavy environments."
    )
    category = Category.TRACKING
    tags = ["tracking", "linking", "nearest-neighbor"]
    environment = GENERAL_ENV

    class Inputs(IOModel):
        objects_csv: Annotated[
            Path,
            GUIMeta(
                display_name="Objects CSV",
                description="Object table from LabelsToObjects.",
                connectable=Connectable.BY_DEFAULT,
            ),
        ]
        max_distance: float = 10.0

    class Outputs(IOModel):
        tracks_csv: Annotated[Path, GUIMeta(display_name="Tracks CSV")] = Template(
            "{objects_csv.stem}_tracks.csv"
        )
        track_count: Annotated[int, GUIMeta(display_name="Track count")]

    def process_row(self, arguments: Arguments, *, context: Any = None) -> Any:
        import numpy as np
        with Path(arguments.objects_csv).open(newline="") as handle:
            objects = sorted(
                csv.DictReader(handle),
                key=lambda row: (int(float(row["frame"])), int(float(row["label"]))),
            )
        rows = []
        active: dict[int, tuple[int, float, float]] = {}
        next_track_id = 1
        max_distance = float(arguments.max_distance)

        frames = sorted({int(float(row["frame"])) for row in objects})
        for frame in frames:
            frame_objects = [row for row in objects if int(float(row["frame"])) == frame]
            used_tracks: set[int] = set()
            for obj in frame_objects:
                obj_y = float(obj["y"])
                obj_x = float(obj["x"])
                best_track = None
                best_distance = float("inf")
                for track_id, (last_frame, y, x) in active.items():
                    if track_id in used_tracks or int(frame) != last_frame + 1:
                        continue
                    distance = float(np.hypot(obj_y - y, obj_x - x))
                    if distance <= max_distance and distance < best_distance:
                        best_track = track_id
                        best_distance = distance
                if best_track is None:
                    best_track = next_track_id
                    next_track_id += 1
                used_tracks.add(best_track)
                active[best_track] = (int(frame), obj_y, obj_x)
                rows.append(
                    {
                        "track_id": best_track,
                        "frame": int(frame),
                        "label": int(float(obj["label"])),
                        "y": obj_y,
                        "x": obj_x,
                        "area": int(float(obj["area"])),
                    }
                )

        output = Path(arguments.tracks_csv)
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("w", newline="") as handle:
            writer = csv.DictWriter(
                handle, fieldnames=["track_id", "frame", "label", "y", "x", "area"]
            )
            writer.writeheader()
            writer.writerows(rows)
        return self.Outputs(
            tracks_csv=output,
            track_count=len({row["track_id"] for row in rows}),
        )
