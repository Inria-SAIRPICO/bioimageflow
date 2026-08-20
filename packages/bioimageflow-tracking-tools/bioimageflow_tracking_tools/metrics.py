"""Track-level motion metrics."""

from pathlib import Path
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
        source_label_image: Annotated[
            Path | None,
            GUIMeta(display_name="Source label image"),
        ] = None
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
        identity_columns = ["track_id", "frame"]
        group_columns = ["track_id"]
        if "source_label_image" in data.columns:
            identity_columns.insert(0, "source_label_image")
            group_columns.insert(0, "source_label_image")
        if data.duplicated(identity_columns).any():
            raise ValueError(
                "TrackMetrics requires at most one observation per track and frame."
            )

        rows: list[dict[str, int | float | Any]] = []
        grouper: str | list[str] = (
            group_columns[0] if len(group_columns) == 1 else group_columns
        )
        for group_key, group in data.groupby(grouper, sort=True):
            if len(group_columns) == 1:
                source_label_image = None
                track_id = group_key
            else:
                source_label_image, track_id = group_key
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
                    "source_label_image": source_label_image,
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

        if "source_label_image" in data.columns:
            rows_by_source: dict[Any, list[dict[str, int | float | Any]]] = {}
            for row in rows:
                rows_by_source.setdefault(row["source_label_image"], []).append(row)
        else:
            rows_by_source = {None: rows}
        for source_rows in rows_by_source.values():
            if not source_rows:
                continue
            track_count = len(source_rows)
            mean_track_length = float(
                np.mean([row["track_length"] for row in source_rows])
            )
            for row in source_rows:
                row["track_count"] = track_count
                row["mean_track_length"] = mean_track_length
        columns = [
            "source_label_image",
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
