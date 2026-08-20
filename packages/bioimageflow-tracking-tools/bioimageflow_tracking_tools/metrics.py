"""Track-level motion metrics."""

from typing import Annotated, Any

from bioimageflow import DataFrameTool
from bioimageflow_core import Category, GUIMeta, IOModel

from ._validation import validate_tracking_columns


class TrackMetrics(DataFrameTool):
    """Compute unambiguous per-track motion and area metrics."""

    display_name = "Track Metrics"
    documentation = (
        "Compute per-track duration, path, displacement, speed, and mean area."
    )
    category = Category.MEASUREMENT
    tags = ["tracking", "metrics"]

    class Inputs(IOModel):
        pass

    class Outputs(IOModel):
        track_id: Annotated[int, GUIMeta(display_name="Track ID")]
        track_length: Annotated[int, GUIMeta(display_name="Track length")]
        duration: Annotated[int, GUIMeta(display_name="Duration")]
        start_frame: Annotated[int, GUIMeta(display_name="Start frame")]
        end_frame: Annotated[int, GUIMeta(display_name="End frame")]
        path_length: Annotated[float, GUIMeta(display_name="Path length")]
        net_displacement: Annotated[float, GUIMeta(display_name="Net displacement")]
        net_speed: Annotated[float, GUIMeta(display_name="Net speed")]
        mean_step_speed: Annotated[float, GUIMeta(display_name="Mean step speed")]
        mean_area: Annotated[float, GUIMeta(display_name="Mean area")]
        track_count: Annotated[int, GUIMeta(display_name="Track count")]
        mean_track_length: Annotated[float, GUIMeta(display_name="Mean track length")]

    def transform(self, df: Any, arguments: Any) -> Any:
        import numpy as np
        import pandas as pd

        data = validate_tracking_columns(
            df,
            tool_name="TrackMetrics",
            require_area=True,
        )
        if data.duplicated(["track_id", "frame"]).any():
            raise ValueError(
                "TrackMetrics requires at most one observation per track and frame."
            )

        rows: list[dict[str, int | float]] = []
        for track_id, group in data.groupby("track_id", sort=True):
            group = group.sort_values("frame", kind="stable")
            frames = group["frame"].to_numpy(dtype=np.int64)
            points = group[["y", "x"]].to_numpy(dtype=float)
            deltas = np.diff(points, axis=0)
            step_distances = np.linalg.norm(deltas, axis=1)
            frame_deltas = np.diff(frames)
            start_frame = int(frames[0])
            end_frame = int(frames[-1])
            duration = end_frame - start_frame
            path_length = float(step_distances.sum())
            net_displacement = float(np.linalg.norm(points[-1] - points[0]))
            mean_step_speed = (
                float(np.mean(step_distances / frame_deltas))
                if len(step_distances)
                else 0.0
            )
            rows.append(
                {
                    "track_id": int(track_id),
                    "track_length": int(len(group)),
                    "duration": duration,
                    "start_frame": start_frame,
                    "end_frame": end_frame,
                    "path_length": path_length,
                    "net_displacement": net_displacement,
                    "net_speed": net_displacement / duration if duration else 0.0,
                    "mean_step_speed": mean_step_speed,
                    "mean_area": float(group["area"].mean()),
                }
            )

        track_count = len(rows)
        mean_track_length = (
            float(np.mean([row["track_length"] for row in rows])) if rows else 0.0
        )
        for row in rows:
            row["track_count"] = track_count
            row["mean_track_length"] = mean_track_length
        columns = [
            "track_id",
            "track_length",
            "duration",
            "start_frame",
            "end_frame",
            "path_length",
            "net_displacement",
            "net_speed",
            "mean_step_speed",
            "mean_area",
            "track_count",
            "mean_track_length",
        ]
        return pd.DataFrame(rows, columns=pd.Index(columns))
