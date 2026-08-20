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

        result["_bif_input_order"] = np.arange(len(result))
        result = result.sort_values(
            ["frame", "label", "_bif_input_order"], kind="stable"
        )
        assignments = pd.Series(index=result.index, dtype=np.int64)
        next_track_id = 1
        previous_frame: int | None = None
        previous_indices: list[Any] = []

        for frame_value, frame_objects in result.groupby("frame", sort=True):
            frame = int(frame_value)
            current_indices = frame_objects.index.tolist()
            current_tracks: dict[Any, int] = {}
            if previous_frame is not None and frame == previous_frame + 1:
                previous_points = result.loc[previous_indices, ["y", "x"]].to_numpy(
                    float
                )
                current_points = frame_objects[["y", "x"]].to_numpy(float)
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
                        current_index = current_indices[current_column]
                        current_tracks[current_index] = int(
                            assignments.loc[previous_indices[previous_row]]
                        )

            for index in current_indices:
                if index not in current_tracks:
                    current_tracks[index] = next_track_id
                    next_track_id += 1
                assignments.loc[index] = current_tracks[index]
            previous_frame = frame
            previous_indices = current_indices

        result["track_id"] = assignments.astype(np.int64)
        result["track_count"] = int(result["track_id"].nunique())
        return result.sort_values("_bif_input_order").drop(columns="_bif_input_order")
