"""Lineage-aware tracking through LapTrack's native dataframe API."""

from pathlib import Path
from typing import Annotated, Any

from bioimageflow_core import (
    Arguments,
    Category,
    Connectable,
    EnvironmentSpec,
    GUIMeta,
    IOModel,
    ProcessingTool,
    RowConsumption,
)


laptrack_env = EnvironmentSpec(
    name="tracking-laptrack",
    dependencies={
        "python": "3.11",
        "pip": [
            "laptrack==0.17.1",
            "numpy==2.2.6",
            "pandas==2.3.3",
            "scipy==1.16.3",
        ],
    },
)


class LapTrackLink(ProcessingTool):
    """Link canonical object rows into tracks and division lineages."""

    row_consumption = RowConsumption.COLLECTIVE
    display_name = "LapTrack Link"
    documentation = (
        "Link centroid objects with LapTrack gap closing and optional divisions. "
        "Merges are intentionally disabled so every lineage has at most one parent."
    )
    category = Category.TRACKING
    tags = ["tracking", "laptrack", "lineage", "division", "gap closing"]
    environment = laptrack_env

    class Inputs(IOModel):
        source_label_image: Annotated[
            Path | None,
            GUIMeta("Source labels", connectable=Connectable.BY_DEFAULT),
        ] = None
        frame: Annotated[int, GUIMeta("Frame", connectable=Connectable.BY_DEFAULT)]
        label: Annotated[int, GUIMeta("Label", connectable=Connectable.BY_DEFAULT)]
        y: Annotated[float, GUIMeta("Y", connectable=Connectable.BY_DEFAULT)]
        x: Annotated[float, GUIMeta("X", connectable=Connectable.BY_DEFAULT)]
        area: Annotated[
            float | None,
            GUIMeta("Area", connectable=Connectable.BY_DEFAULT),
        ] = None
        max_link_distance: Annotated[
            float,
            GUIMeta(display_name="Maximum link distance", min=0.000001),
        ] = 15.0
        gap_closing_distance: Annotated[
            float | None,
            GUIMeta(display_name="Gap-closing distance", min=0.000001),
        ] = None
        gap_closing_max_frames: Annotated[
            int,
            GUIMeta(display_name="Maximum gap frames", min=1),
        ] = 2
        division_distance: Annotated[
            float | None,
            GUIMeta(display_name="Division distance", min=0.000001),
        ] = None

    class Outputs(IOModel):
        source_label_image: Annotated[Path | None, GUIMeta("Source labels")]
        frame: Annotated[int, GUIMeta("Frame")]
        label: Annotated[int, GUIMeta("Label")]
        y: Annotated[float, GUIMeta("Y")]
        x: Annotated[float, GUIMeta("X")]
        area: Annotated[float | None, GUIMeta("Area")]
        track_id: Annotated[int, GUIMeta("Track ID")]
        lineage_id: Annotated[int, GUIMeta("Lineage ID")]
        parent_track_id: Annotated[int | None, GUIMeta("Parent track ID")]
        generation: Annotated[int, GUIMeta("Generation")]
        track_count: Annotated[int, GUIMeta("Track count")]
        division_count: Annotated[int, GUIMeta("Division count")]

    def process_batch(
        self,
        arguments_list: list[Arguments],
        *,
        context: Any = None,
    ) -> Any:
        import pandas as pd

        if not arguments_list:
            return []
        rows: list[dict[str, Any]] = []
        for position, arguments in enumerate(arguments_list):
            frame = _integral(arguments.frame, "frame", minimum=0)
            label = _integral(arguments.label, "label", minimum=1)
            y = _finite(arguments.y, "y")
            x = _finite(arguments.x, "x")
            area = None if arguments.area is None else _finite(arguments.area, "area")
            if area is not None and area < 0:
                raise ValueError("area must be non-negative when provided.")
            source = (
                None
                if arguments.source_label_image is None
                else Path(arguments.source_label_image)
            )
            rows.append(
                {
                    "input_position": position,
                    "source_label_image": source,
                    "frame": frame,
                    "label": label,
                    "y": y,
                    "x": x,
                    "area": area,
                }
            )
        objects = pd.DataFrame(rows)
        identity = ["source_label_image", "frame", "label"]
        if objects.duplicated(identity).any():
            raise ValueError(
                "LapTrackLink requires unique (source_label_image, frame, label) rows."
            )

        grouped_positions: dict[Path | None, list[int]] = {}
        for row in rows:
            grouped_positions.setdefault(row["source_label_image"], []).append(
                int(row["input_position"])
            )
        output_by_position: dict[int, Any] = {}
        from laptrack import LapTrack  # type: ignore

        for source, positions in grouped_positions.items():
            first_arguments = arguments_list[positions[0]]
            max_link = _positive(first_arguments.max_link_distance, "max_link_distance")
            gap_distance = _optional_positive(
                first_arguments.gap_closing_distance,
                "gap_closing_distance",
            )
            max_gap_frames = _integral(
                first_arguments.gap_closing_max_frames,
                "gap_closing_max_frames",
                minimum=1,
            )
            division_distance = _optional_positive(
                first_arguments.division_distance,
                "division_distance",
            )
            expected_config = (
                max_link,
                gap_distance,
                max_gap_frames,
                division_distance,
            )
            for position in positions[1:]:
                row_arguments = arguments_list[position]
                row_config = (
                    _positive(row_arguments.max_link_distance, "max_link_distance"),
                    _optional_positive(
                        row_arguments.gap_closing_distance,
                        "gap_closing_distance",
                    ),
                    _integral(
                        row_arguments.gap_closing_max_frames,
                        "gap_closing_max_frames",
                        minimum=1,
                    ),
                    _optional_positive(
                        row_arguments.division_distance,
                        "division_distance",
                    ),
                )
                if row_config != expected_config:
                    raise ValueError(
                        "LapTrackLink configuration must be constant within each source image."
                    )

            group = objects.iloc[positions].copy()
            group = group.sort_values(
                ["frame", "label", "input_position"], kind="stable"
            ).reset_index(drop=True)
            tracker = LapTrack(
                metric="sqeuclidean",
                cutoff=max_link**2,
                gap_closing_metric="sqeuclidean",
                gap_closing_cutoff=(
                    False if gap_distance is None else gap_distance**2
                ),
                gap_closing_max_frame_count=max_gap_frames,
                splitting_metric="sqeuclidean",
                splitting_cutoff=(
                    False if division_distance is None else division_distance**2
                ),
                merging_cutoff=False,
            )
            tracked, splits, merges = tracker.predict_dataframe(
                group,
                coordinate_cols=["y", "x"],
                frame_col="frame",
                only_coordinate_cols=False,
            )
            if len(merges):
                raise RuntimeError("LapTrack returned merges although merging is disabled.")
            tracked = tracked.reset_index(drop=True)
            if "input_position" not in tracked:
                raise RuntimeError("LapTrack did not preserve the object identity column.")
            upstream_track_ids = sorted(int(value) for value in tracked["track_id"].unique())
            track_id_map = {
                upstream: normalized
                for normalized, upstream in enumerate(upstream_track_ids, start=1)
            }
            upstream_tree_ids = sorted(int(value) for value in tracked["tree_id"].unique())
            lineage_id_map = {
                upstream: normalized
                for normalized, upstream in enumerate(upstream_tree_ids, start=1)
            }
            parent_by_track: dict[int, int] = {}
            for split in splits.to_dict("records"):
                parent = int(split["parent_track_id"])
                child = int(split["child_track_id"])
                previous = parent_by_track.setdefault(child, parent)
                if previous != parent:
                    raise RuntimeError("LapTrack produced more than one parent for a track.")

            generation_cache: dict[int, int] = {}

            def generation(track_id: int, visiting: set[int] | None = None) -> int:
                if track_id in generation_cache:
                    return generation_cache[track_id]
                parent = parent_by_track.get(track_id)
                if parent is None:
                    generation_cache[track_id] = 0
                    return 0
                active = set() if visiting is None else set(visiting)
                if track_id in active:
                    raise RuntimeError("LapTrack returned a cyclic lineage.")
                active.add(track_id)
                value = generation(parent, active) + 1
                generation_cache[track_id] = value
                return value

            track_count = len(track_id_map)
            division_count = len(set(parent_by_track.values()))
            for tracked_row in tracked.to_dict("records"):
                upstream_track = int(tracked_row["track_id"])
                upstream_lineage = int(tracked_row["tree_id"])
                parent = parent_by_track.get(upstream_track)
                position = int(tracked_row["input_position"])
                output_by_position[position] = self.Outputs(
                    source_label_image=source,
                    frame=int(tracked_row["frame"]),
                    label=int(tracked_row["label"]),
                    y=float(tracked_row["y"]),
                    x=float(tracked_row["x"]),
                    area=(
                        None
                        if pd.isna(tracked_row["area"])
                        else float(tracked_row["area"])
                    ),
                    track_id=track_id_map[upstream_track],
                    lineage_id=lineage_id_map[upstream_lineage],
                    parent_track_id=(None if parent is None else track_id_map[parent]),
                    generation=generation(upstream_track),
                    track_count=track_count,
                    division_count=division_count,
                )

        if set(output_by_position) != set(range(len(arguments_list))):
            raise RuntimeError("LapTrack did not return exactly one row per input object.")
        return [[output_by_position[position]] for position in range(len(arguments_list))]


def _finite(value: Any, name: str) -> float:
    import math

    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{name} must be finite.")
    return result


def _positive(value: Any, name: str) -> float:
    result = _finite(value, name)
    if result <= 0:
        raise ValueError(f"{name} must be greater than zero.")
    return result


def _optional_positive(value: Any, name: str) -> float | None:
    return None if value is None else _positive(value, name)


def _integral(value: Any, name: str, *, minimum: int) -> int:
    import numbers

    if isinstance(value, bool) or not isinstance(value, numbers.Integral):
        raise ValueError(f"{name} must be an integer greater than or equal to {minimum}.")
    result = int(value)
    if result < minimum:
        raise ValueError(f"{name} must be greater than or equal to {minimum}.")
    return result
