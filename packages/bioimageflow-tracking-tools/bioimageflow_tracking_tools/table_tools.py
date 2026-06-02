"""Tracking table filters, validation, rendering, and summaries."""

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


def _read_rows(path: Path) -> list[dict[str, str]]:
    with Path(path).open(newline="") as handle:
        return list(csv.DictReader(handle))


def _write_rows(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> Path:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    return output


def _float(row: dict[str, Any], column: str, default: float | None = None) -> float:
    value = row.get(column, "")
    if value in {"", None}:
        if default is None:
            raise ValueError(f"Track table row is missing required column {column!r}.")
        return default
    return float(value)


def _int(row: dict[str, Any], column: str, default: int | None = None) -> int:
    float_default = float(default) if default is not None else None
    return int(round(_float(row, column, float_default)))


def _argument(arguments: Arguments, name: str, default: Any) -> Any:
    return getattr(arguments, name, default)


class FilterObjects(ProcessingTool):
    """Filter object tables by area, frame, intensity, and position."""

    display_name = "Filter Objects"
    documentation = "Filter object tables by area, frame, intensity, and position."
    category = Category.TRACKING
    tags = ["tracking", "objects", "filter"]
    environment = GENERAL_ENV

    class Inputs(IOModel):
        objects_csv: Annotated[Path, GUIMeta("Objects CSV", connectable=Connectable.BY_DEFAULT)]
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

    class Outputs(IOModel):
        filtered_objects_csv: Annotated[Path, GUIMeta("Filtered objects CSV")] = Template(
            "{objects_csv.stem}_filtered.csv"
        )
        object_count: Annotated[int, GUIMeta("Object count")]

    def process_row(self, arguments: Arguments, *, context: Any = None) -> Any:
        rows = _read_rows(arguments.objects_csv)

        def keep(row: dict[str, str]) -> bool:
            checks = [
                ("area", _argument(arguments, "min_area", None), _argument(arguments, "max_area", None)),
                ("frame", _argument(arguments, "min_frame", None), _argument(arguments, "max_frame", None)),
                (
                    "intensity",
                    _argument(arguments, "min_intensity", None),
                    _argument(arguments, "max_intensity", None),
                ),
                ("y", _argument(arguments, "min_y", None), _argument(arguments, "max_y", None)),
                ("x", _argument(arguments, "min_x", None), _argument(arguments, "max_x", None)),
            ]
            for column, minimum, maximum in checks:
                if column not in row or row[column] in {"", None}:
                    continue
                value = float(row[column])
                if minimum is not None and value < float(minimum):
                    return False
                if maximum is not None and value > float(maximum):
                    return False
            return True

        filtered = [row for row in rows if keep(row)]
        fieldnames = list(rows[0]) if rows else ["frame", "label", "y", "x", "area"]
        output = _write_rows(arguments.filtered_objects_csv, filtered, fieldnames)
        return self.Outputs(filtered_objects_csv=output, object_count=len(filtered))


class TracksToLabels(ProcessingTool):
    """Render track IDs into a label stack using source object labels."""

    display_name = "Tracks To Labels"
    documentation = "Render track IDs back into a label stack."
    category = Category.TRACKING
    tags = ["tracking", "labels", "render"]
    environment = GENERAL_ENV

    class Inputs(IOModel):
        tracks_csv: Annotated[Path, GUIMeta("Tracks CSV", connectable=Connectable.BY_DEFAULT)]
        label_image: Annotated[
            Path,
            ImageSpec(semantics={Semantic.LABEL}, layouts={Layout.PLANAR, Layout.PLANAR_TIME}),
            GUIMeta("Source labels", connectable=Connectable.BY_DEFAULT),
        ]

    class Outputs(IOModel):
        output_label_image: Annotated[
            Path,
            ImageSpec(semantics={Semantic.LABEL}, layouts={Layout.PLANAR_TIME}),
            GUIMeta("Track labels"),
        ] = Template("{label_image.stem}_tracks.tif")
        track_count: Annotated[int, GUIMeta("Track count")]

    def process_row(self, arguments: Arguments, *, context: Any = None) -> Any:
        import imageio.v3 as iio
        import numpy as np

        tracks = _read_rows(arguments.tracks_csv)
        source = iio.imread(arguments.label_image)
        if source.ndim == 2:
            source = source[np.newaxis, ...]
        output_image = np.zeros(source.shape, dtype=np.uint16)
        for row in tracks:
            frame = _int(row, "frame")
            label = _int(row, "label")
            track_id = _int(row, "track_id")
            if 0 <= frame < source.shape[0]:
                output_image[frame][source[frame] == label] = track_id
        output = Path(arguments.output_label_image)
        output.parent.mkdir(parents=True, exist_ok=True)
        iio.imwrite(output, output_image, photometric="minisblack")
        return self.Outputs(
            output_label_image=output,
            track_count=len({_int(row, "track_id") for row in tracks}),
        )


class TrackTableValidate(ProcessingTool):
    """Validate required tracking columns and basic table consistency."""

    display_name = "Track Table Validate"
    documentation = "Validate required columns, frame order, and track IDs."
    category = Category.TRACKING
    tags = ["tracking", "validation", "tables"]
    environment = GENERAL_ENV

    class Inputs(IOModel):
        tracks_csv: Annotated[Path, GUIMeta("Tracks CSV", connectable=Connectable.BY_DEFAULT)]

    class Outputs(IOModel):
        validation_csv: Annotated[Path, GUIMeta("Validation CSV")] = Template(
            "{tracks_csv.stem}_validation.csv"
        )
        valid: Annotated[bool, GUIMeta("Valid")]
        error_count: Annotated[int, GUIMeta("Error count")]

    def process_row(self, arguments: Arguments, *, context: Any = None) -> Any:
        rows = _read_rows(arguments.tracks_csv)
        required = {"track_id", "frame", "label", "y", "x"}
        present = set(rows[0]) if rows else set()
        errors = [
            {"severity": "error", "message": f"missing required column: {column}"}
            for column in sorted(required - present)
        ]
        seen_track_frames: set[tuple[int, int]] = set()
        frames_by_track: dict[int, list[int]] = {}
        for row in rows:
            try:
                track_id = _int(row, "track_id")
                frame = _int(row, "frame")
                _int(row, "label")
                _float(row, "y")
                _float(row, "x")
            except ValueError as exc:
                errors.append({"severity": "error", "message": str(exc)})
                continue
            key = (track_id, frame)
            if key in seen_track_frames:
                errors.append(
                    {
                        "severity": "error",
                        "message": f"duplicate row for track_id {track_id} frame {frame}",
                    }
                )
            seen_track_frames.add(key)
            frames_by_track.setdefault(track_id, []).append(frame)
        for track_id, frames in frames_by_track.items():
            if frames != sorted(frames):
                errors.append(
                    {
                        "severity": "error",
                        "message": f"frames are not sorted for track_id {track_id}",
                    }
                )
        output = _write_rows(arguments.validation_csv, errors, ["severity", "message"])
        return self.Outputs(
            validation_csv=output,
            valid=len(errors) == 0,
            error_count=len(errors),
        )


def _track_groups(rows: list[dict[str, str]]) -> dict[int, list[dict[str, str]]]:
    groups: dict[int, list[dict[str, str]]] = {}
    for row in rows:
        groups.setdefault(_int(row, "track_id"), []).append(row)
    return {
        track_id: sorted(group, key=lambda row: _int(row, "frame"))
        for track_id, group in groups.items()
    }


class TrackSummary(ProcessingTool):
    """Compute per-track duration, displacement, and speed."""

    display_name = "Track Summary"
    documentation = "Summarize per-track duration, displacement, speed, and frame bounds."
    category = Category.MEASUREMENT
    tags = ["tracking", "summary", "metrics"]
    environment = GENERAL_ENV

    class Inputs(IOModel):
        tracks_csv: Annotated[Path, GUIMeta("Tracks CSV", connectable=Connectable.BY_DEFAULT)]

    class Outputs(IOModel):
        summary_csv: Annotated[Path, GUIMeta("Track summary CSV")] = Template(
            "{tracks_csv.stem}_summary.csv"
        )
        track_count: Annotated[int, GUIMeta("Track count")]

    def process_row(self, arguments: Arguments, *, context: Any = None) -> Any:
        import numpy as np

        rows = _read_rows(arguments.tracks_csv)
        summaries = []
        for track_id, group in sorted(_track_groups(rows).items()):
            first = group[0]
            last = group[-1]
            start = _int(first, "frame")
            end = _int(last, "frame")
            duration = end - start + 1
            displacement = float(
                np.hypot(_float(last, "y") - _float(first, "y"), _float(last, "x") - _float(first, "x"))
            )
            summaries.append(
                {
                    "track_id": track_id,
                    "track_length": len(group),
                    "duration": duration,
                    "start_frame": start,
                    "end_frame": end,
                    "displacement": displacement,
                    "mean_speed": displacement / max(1, duration - 1),
                }
            )
        output = _write_rows(
            arguments.summary_csv,
            summaries,
            [
                "track_id",
                "track_length",
                "duration",
                "start_frame",
                "end_frame",
                "displacement",
                "mean_speed",
            ],
        )
        return self.Outputs(summary_csv=output, track_count=len(summaries))


class TrackQualityMetrics(ProcessingTool):
    """Compute simple quality metrics for linked track tables."""

    display_name = "Track Quality Metrics"
    documentation = "Compute gap counts, split/merge flags, and short-track fraction."
    category = Category.MEASUREMENT
    tags = ["tracking", "quality", "metrics"]
    environment = GENERAL_ENV

    class Inputs(IOModel):
        tracks_csv: Annotated[Path, GUIMeta("Tracks CSV", connectable=Connectable.BY_DEFAULT)]
        min_track_length: int = 3

    class Outputs(IOModel):
        quality_csv: Annotated[Path, GUIMeta("Track quality CSV")] = Template(
            "{tracks_csv.stem}_quality.csv"
        )
        track_count: Annotated[int, GUIMeta("Track count")]
        gap_count: Annotated[int, GUIMeta("Gap count")]
        split_count: Annotated[int, GUIMeta("Split count")]
        merge_count: Annotated[int, GUIMeta("Merge count")]
        short_track_fraction: Annotated[float, GUIMeta("Short-track fraction")]

    def process_row(self, arguments: Arguments, *, context: Any = None) -> Any:
        rows = _read_rows(arguments.tracks_csv)
        groups = _track_groups(rows)
        gap_count = 0
        short_count = 0
        for group in groups.values():
            frames = [_int(row, "frame") for row in group]
            gap_count += sum(
                max(0, frames[index + 1] - frames[index] - 1)
                for index in range(len(frames) - 1)
            )
            if len(group) < int(arguments.min_track_length):
                short_count += 1
        track_frame_counts: dict[tuple[int, int], int] = {}
        object_frame_counts: dict[tuple[int, int], int] = {}
        for row in rows:
            track_key = (_int(row, "track_id"), _int(row, "frame"))
            object_key = (_int(row, "label"), _int(row, "frame"))
            track_frame_counts[track_key] = track_frame_counts.get(track_key, 0) + 1
            object_frame_counts[object_key] = object_frame_counts.get(object_key, 0) + 1
        split_count = sum(1 for count in track_frame_counts.values() if count > 1)
        merge_count = sum(1 for count in object_frame_counts.values() if count > 1)
        track_count = len(groups)
        short_fraction = short_count / track_count if track_count else 0.0
        row = {
            "track_count": track_count,
            "gap_count": gap_count,
            "split_count": split_count,
            "merge_count": merge_count,
            "short_track_fraction": short_fraction,
        }
        output = _write_rows(
            arguments.quality_csv,
            [row],
            [
                "track_count",
                "gap_count",
                "split_count",
                "merge_count",
                "short_track_fraction",
            ],
        )
        return self.Outputs(
            quality_csv=output,
            track_count=track_count,
            gap_count=gap_count,
            split_count=split_count,
            merge_count=merge_count,
            short_track_fraction=short_fraction,
        )
