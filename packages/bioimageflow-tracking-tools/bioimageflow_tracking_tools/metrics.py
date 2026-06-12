"""Track-level metrics."""

from typing import Annotated, Any

from bioimageflow import DataFrameTool
from bioimageflow_core import (
    Category,
    GUIMeta,
    IOModel,
)


def _require_columns(df: Any, columns: set[str], tool_name: str) -> None:
    missing = sorted(columns - set(df.columns))
    if missing:
        raise ValueError(
            f"{tool_name} input table is missing required column(s): "
            f"{', '.join(repr(column) for column in missing)}."
        )


class TrackMetrics(DataFrameTool):
    """Compute track length, displacement, and mean speed summaries."""

    display_name = "Track Metrics"
    documentation = "Compute basic per-track metrics from linked object tables."
    category = Category.MEASUREMENT
    tags = ["tracking", "metrics"]

    class Inputs(IOModel):
        pass

    class Outputs(IOModel):
        track_id: Annotated[int, GUIMeta(display_name="Track ID")]
        track_length: Annotated[int, GUIMeta(display_name="Track length")]
        start_frame: Annotated[int, GUIMeta(display_name="Start frame")]
        end_frame: Annotated[int, GUIMeta(display_name="End frame")]
        displacement: Annotated[float, GUIMeta(display_name="Displacement")]
        mean_speed: Annotated[float, GUIMeta(display_name="Mean speed")]
        mean_area: Annotated[float, GUIMeta(display_name="Mean area")]
        track_count: Annotated[int, GUIMeta(display_name="Track count")]
        mean_track_length: Annotated[float, GUIMeta(display_name="Mean track length")]

    def transform(self, df: Any, arguments: Any) -> Any:
        import numpy as np
        import pandas as pd

        _require_columns(df, {"track_id", "frame", "y", "x", "area"}, "TrackMetrics")
        rows = []
        track_ids = sorted({int(float(track_id)) for track_id in df["track_id"]})
        for track_id in track_ids:
            group = df[df["track_id"].astype(float).astype(int) == track_id].copy()
            group["_bif_frame_sort"] = group["frame"].astype(float).astype(int)
            group = group.sort_values("_bif_frame_sort")
            first = group.iloc[0]
            last = group.iloc[-1]
            displacement = float(
                np.hypot(
                    float(last["y"]) - float(first["y"]),
                    float(last["x"]) - float(first["x"]),
                )
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
                    "mean_area": float(group["area"].astype(float).mean()),
                }
            )
        mean_length = (
            sum(float(row["track_length"]) for row in rows) / len(rows) if rows else 0.0
        )
        for row in rows:
            row["track_count"] = len(rows)
            row["mean_track_length"] = mean_length
        columns = [
            "track_id",
            "track_length",
            "start_frame",
            "end_frame",
            "displacement",
            "mean_speed",
            "mean_area",
            "track_count",
            "mean_track_length",
        ]
        return pd.DataFrame(rows, columns=pd.Index(columns))
