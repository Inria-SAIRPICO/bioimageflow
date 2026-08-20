"""Tracking table filtering, validation, and quality metrics."""

from pathlib import Path
from typing import Annotated, Any

from bioimageflow import DataFrameTool, Passthrough
from bioimageflow_core import Category, GUIMeta, IOModel

from ._validation import finite_float, require_columns, validate_tracking_columns


class FilterObjects(DataFrameTool):
    """Filter object rows using only the requested bounds."""

    display_name = "Filter Objects"
    documentation = "Filter object tables by area, frame, intensity, and position."
    category = Category.TRACKING
    tags = ["tracking", "objects", "filter"]

    class Inputs(IOModel):
        min_area: float | None = None
        max_area: float | None = None
        min_frame: int | None = None
        max_frame: int | None = None
        min_intensity: float | None = None
        max_intensity: float | None = None
        min_y: float | None = None
        max_y: float | None = None
        min_x: float | None = None
        max_x: float | None = None

    class Outputs(Passthrough):
        object_count: Annotated[int, GUIMeta("Object count")]

    def transform(self, df: Any, arguments: Any) -> Any:
        import numpy as np
        import pandas as pd

        checks = [
            (
                "area",
                getattr(arguments, "min_area", None),
                getattr(arguments, "max_area", None),
            ),
            (
                "frame",
                getattr(arguments, "min_frame", None),
                getattr(arguments, "max_frame", None),
            ),
            (
                "intensity",
                getattr(arguments, "min_intensity", None),
                getattr(arguments, "max_intensity", None),
            ),
            ("y", getattr(arguments, "min_y", None), getattr(arguments, "max_y", None)),
            ("x", getattr(arguments, "min_x", None), getattr(arguments, "max_x", None)),
        ]
        active = [
            (column, minimum, maximum)
            for column, minimum, maximum in checks
            if minimum is not None or maximum is not None
        ]
        require_columns(df, {column for column, _, _ in active}, "FilterObjects")

        mask = pd.Series(True, index=df.index, dtype=bool)
        for column, minimum, maximum in active:
            lower = (
                finite_float(minimum, f"min_{column}") if minimum is not None else None
            )
            upper = (
                finite_float(maximum, f"max_{column}") if maximum is not None else None
            )
            if lower is not None and upper is not None and lower > upper:
                raise ValueError(f"min_{column} must be <= max_{column}.")
            try:
                values = np.asarray(
                    pd.to_numeric(df[column], errors="raise"), dtype=np.float64
                )
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f"FilterObjects column {column!r} must be numeric."
                ) from exc
            column_mask = np.isfinite(values)
            if lower is not None:
                column_mask &= values >= lower
            if upper is not None:
                column_mask &= values <= upper
            mask &= column_mask

        result = df.loc[mask].copy()
        result["object_count"] = len(result)
        return result


class TrackTableValidate(DataFrameTool):
    """Report invalid values and inconsistent object assignments in a track table."""

    display_name = "Track Table Validate"
    documentation = (
        "Validate required columns, values, ordering, and object assignments."
    )
    category = Category.TRACKING
    tags = ["tracking", "validation", "tables"]

    class Inputs(IOModel):
        pass

    class Outputs(IOModel):
        source_label_image: Annotated[Path | None, GUIMeta("Source label image")] = None
        severity: Annotated[str, GUIMeta("Severity")]
        message: Annotated[str, GUIMeta("Message")]
        valid: Annotated[bool, GUIMeta("Valid")]
        error_count: Annotated[int, GUIMeta("Error count")]

    def transform(self, df: Any, arguments: Any) -> Any:
        import pandas as pd

        required = {"track_id", "frame", "label", "y", "x"}
        missing = sorted(required - set(df.columns))
        errors: list[dict[str, Any]] = [
            {"severity": "error", "message": f"missing required column: {column}"}
            for column in missing
        ]
        data = None
        if not missing:
            try:
                data = validate_tracking_columns(
                    df,
                    tool_name="TrackTableValidate",
                    require_label=True,
                )
            except ValueError as exc:
                errors.append({"severity": "error", "message": str(exc)})

        if data is not None:
            source_column = (
                ["source_label_image"] if "source_label_image" in data.columns else []
            )
            track_frame_columns = [*source_column, "track_id", "frame"]
            duplicate_track_frames = data.duplicated(track_frame_columns, keep=False)
            for values in (
                data.loc[duplicate_track_frames, track_frame_columns]
                .drop_duplicates()
                .itertuples(index=False, name=None)
            ):
                source = values[0] if source_column else None
                track_id, frame = values[-2:]
                errors.append(
                    {
                        "source_label_image": source,
                        "severity": "error",
                        "message": f"duplicate row for track_id {track_id} frame {frame}",
                    }
                )
            object_columns = [*source_column, "frame", "label"]
            duplicate_objects = data.duplicated(object_columns, keep=False)
            for values in (
                data.loc[duplicate_objects, object_columns]
                .drop_duplicates()
                .itertuples(index=False, name=None)
            ):
                source = values[0] if source_column else None
                frame, label = values[-2:]
                errors.append(
                    {
                        "source_label_image": source,
                        "severity": "error",
                        "message": f"object label {label} in frame {frame} has multiple assignments",
                    }
                )
            track_columns = [*source_column, "track_id"]
            grouper: str | list[str] = (
                track_columns[0] if len(track_columns) == 1 else track_columns
            )
            for group_key, group in data.groupby(grouper, sort=True):
                if source_column:
                    source, track_id = group_key
                else:
                    source = None
                    track_id = group_key
                frames = group["frame"].tolist()
                if frames != sorted(frames):
                    errors.append(
                        {
                            "source_label_image": source,
                            "severity": "error",
                            "message": f"frames are not sorted for track_id {track_id}",
                        }
                    )

        if not errors:
            errors = [
                {
                    "source_label_image": None,
                    "severity": "info",
                    "message": "valid",
                }
            ]
        valid = len(errors) == 1 and errors[0]["severity"] == "info"
        error_count = 0 if valid else len(errors)
        return pd.DataFrame(
            [
                {
                    "source_label_image": error.get("source_label_image"),
                    "severity": error["severity"],
                    "message": error["message"],
                    "valid": valid,
                    "error_count": error_count,
                }
                for error in errors
            ]
        )


class TrackQualityMetrics(DataFrameTool):
    """Summarize track continuity and duplicate assignment conflicts."""

    display_name = "Track Quality Metrics"
    documentation = "Compute gaps, duplicate assignments, and short-track fraction."
    category = Category.MEASUREMENT
    tags = ["tracking", "quality", "metrics"]

    class Inputs(IOModel):
        min_track_length: int = 3

    class Outputs(IOModel):
        source_label_image: Annotated[Path | None, GUIMeta("Source label image")] = None
        track_count: Annotated[int, GUIMeta("Track count")]
        gap_count: Annotated[int, GUIMeta("Gap count")]
        duplicate_track_frame_count: Annotated[
            int, GUIMeta("Duplicate track-frame count")
        ]
        object_assignment_conflict_count: Annotated[
            int, GUIMeta("Object assignment conflict count")
        ]
        short_track_fraction: Annotated[float, GUIMeta("Short-track fraction")]

    def transform(self, df: Any, arguments: Any) -> Any:
        import pandas as pd

        minimum = finite_float(
            getattr(arguments, "min_track_length", 3),
            "min_track_length",
        )
        if not minimum.is_integer() or minimum < 1:
            raise ValueError("min_track_length must be a positive integer.")
        min_track_length = int(minimum)
        data = validate_tracking_columns(
            df,
            tool_name="TrackQualityMetrics",
            require_coordinates=False,
            require_label=True,
        )

        if data.empty:
            source_groups = [(None, data)]
        elif "source_label_image" in data.columns:
            source_groups = data.groupby("source_label_image", sort=True)
        else:
            source_groups = [(None, data)]

        rows = []
        for source_label_image, source_data in source_groups:
            gap_count = 0
            short_count = 0
            for _, group in source_data.groupby("track_id", sort=True):
                frames = sorted(set(int(frame) for frame in group["frame"]))
                gap_count += sum(
                    max(0, following - current - 1)
                    for current, following in zip(frames, frames[1:])
                )
                if len(frames) < min_track_length:
                    short_count += 1

            duplicate_track_frame_count = int(
                source_data.duplicated(["track_id", "frame"], keep="first").sum()
            )
            object_assignment_conflict_count = int(
                source_data.duplicated(["frame", "label"], keep="first").sum()
            )
            track_count = int(source_data["track_id"].nunique())
            rows.append(
                {
                    "source_label_image": source_label_image,
                    "track_count": track_count,
                    "gap_count": gap_count,
                    "duplicate_track_frame_count": duplicate_track_frame_count,
                    "object_assignment_conflict_count": object_assignment_conflict_count,
                    "short_track_fraction": short_count / track_count
                    if track_count
                    else 0.0,
                }
            )
        return pd.DataFrame(rows)
