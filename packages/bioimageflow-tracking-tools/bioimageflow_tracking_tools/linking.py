"""Deterministic object-table linking."""

from typing import Annotated, Any

from bioimageflow import DataFrameTool, Passthrough
from bioimageflow_core import Category, GUIMeta, IOModel

from ._validation import finite_float, require_columns


class NearestNeighborLink(DataFrameTool):
    """Link objects in adjacent frames by global minimum-distance assignment."""

    display_name = "Nearest Neighbor Link"
    documentation = "Link objects between adjacent frames with a global one-to-one distance assignment."
    category = Category.TRACKING
    tags = ["tracking", "linking", "nearest-neighbor"]

    class Inputs(IOModel):
        max_distance: Annotated[
            float,
            GUIMeta(
                display_name="Max distance",
                description="Maximum centroid distance allowed between adjacent frames.",
            ),
        ] = 10.0

    class Outputs(Passthrough):
        track_id: Annotated[int, GUIMeta(display_name="Track ID")]
        track_count: Annotated[int, GUIMeta(display_name="Track count")]

    def transform(self, df: Any, arguments: Any) -> Any:
        import numpy as np
        import pandas as pd
        from scipy.optimize import linear_sum_assignment
        from scipy.spatial.distance import cdist

        tool_name = "NearestNeighborLink"
        required = {"frame", "label", "y", "x"}
        require_columns(df, required, tool_name)
        max_distance = finite_float(
            getattr(arguments, "max_distance", 10.0), "max_distance"
        )
        if max_distance < 0:
            raise ValueError("max_distance must be non-negative.")

        result = df.copy()
        for column in required:
            try:
                values = np.asarray(
                    pd.to_numeric(result[column], errors="raise"), dtype=np.float64
                )
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f"{tool_name} column {column!r} must be numeric."
                ) from exc
            if not np.isfinite(values).all():
                raise ValueError(
                    f"{tool_name} column {column!r} must contain only finite values."
                )
            if column in {"frame", "label"}:
                if not np.equal(values, np.floor(values)).all():
                    raise ValueError(
                        f"{tool_name} column {column!r} must contain only integers."
                    )
                minimum = 0 if column == "frame" else 1
                if (values < minimum).any():
                    qualifier = "non-negative" if column == "frame" else "positive"
                    raise ValueError(
                        f"{tool_name} column {column!r} must contain only {qualifier} integers."
                    )
                result[column] = values.astype(np.int64)
            else:
                result[column] = values

        if result.duplicated(["frame", "label"]).any():
            raise ValueError(
                "NearestNeighborLink requires each (frame, label) object to occur exactly once."
            )
        if result.empty:
            result["track_id"] = pd.Series(dtype=np.int64)
            result["track_count"] = pd.Series(dtype=np.int64)
            return result

        frames = result["frame"].to_numpy(dtype=np.int64)
        labels = result["label"].to_numpy(dtype=np.int64)
        input_positions = np.arange(len(result), dtype=np.int64)
        sorted_positions = np.lexsort((input_positions, labels, frames))
        assignments = np.empty(len(result), dtype=np.int64)
        next_track_id = 1
        previous_frame: int | None = None
        previous_positions = np.empty(0, dtype=np.int64)

        for frame_value in np.unique(frames[sorted_positions]):
            frame = int(frame_value)
            current_positions = sorted_positions[frames[sorted_positions] == frame]
            current_tracks: dict[int, int] = {}
            if previous_frame is not None and frame == previous_frame + 1:
                previous_points = result.iloc[previous_positions][["y", "x"]].to_numpy(float)
                current_points = result.iloc[current_positions][["y", "x"]].to_numpy(float)
                distances = cdist(previous_points, current_points)
                # Invalid edges cost more than all valid edges combined, so assignment
                # maximizes the valid link count before minimizing total distance.
                valid_cost = distances / (max_distance + 1.0)
                invalid_cost = float(min(distances.shape) + 1)
                cost = np.where(distances <= max_distance, valid_cost, invalid_cost)
                previous_rows, current_columns = linear_sum_assignment(cost)
                for previous_row, current_column in zip(
                    previous_rows.tolist(), current_columns.tolist(), strict=True
                ):
                    if distances[previous_row, current_column] <= max_distance:
                        current_position = int(current_positions[current_column])
                        current_tracks[current_position] = int(
                            assignments[previous_positions[previous_row]]
                        )

            for position_value in current_positions:
                position = int(position_value)
                if position not in current_tracks:
                    current_tracks[position] = next_track_id
                    next_track_id += 1
                assignments[position] = current_tracks[position]
            previous_frame = frame
            previous_positions = current_positions

        result["track_id"] = assignments
        result["track_count"] = int(result["track_id"].nunique())
        return result
