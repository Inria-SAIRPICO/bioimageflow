"""Greedy nearest-neighbor object linking."""

from typing import Annotated, Any

from bioimageflow import DataFrameTool, Passthrough
from bioimageflow_core import (
    Category,
    EnvironmentSpec,
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


class LinkObjects(DataFrameTool):
    """Link objects frame-to-frame with a lightweight nearest-neighbor method."""

    display_name = "Link Objects"
    documentation = (
        "Greedy nearest-neighbor frame linking. btrack/LapTrack integrations are "
        "optional and reserved for heavy environments."
    )
    category = Category.TRACKING
    tags = ["tracking", "linking", "nearest-neighbor"]

    class Inputs(IOModel):
        max_distance: Annotated[
            float,
            GUIMeta(
                display_name="Max distance",
                description="Maximum centroid distance for linking objects between adjacent frames.",
            ),
        ] = 10.0

    class Outputs(Passthrough):
        track_id: Annotated[int, GUIMeta(display_name="Track ID")]
        track_count: Annotated[int, GUIMeta(display_name="Track count")]

    def transform(self, df: Any, arguments: Any) -> Any:
        import numpy as np

        _require_columns(df, {"frame", "label", "y", "x", "area"}, "LinkObjects")
        if df.empty:
            result = df.copy()
            result["track_id"] = []
            result["track_count"] = []
            return result

        result = df.copy()
        result["_bif_frame_sort"] = result["frame"].astype(float).astype(int)
        result["_bif_label_sort"] = result["label"].astype(float).astype(int)
        result = result.sort_values(["_bif_frame_sort", "_bif_label_sort"])

        active: dict[int, tuple[int, float, float]] = {}
        next_track_id = 1
        assignments: dict[Any, int] = {}
        max_distance = float(getattr(arguments, "max_distance", 10.0))

        frames = sorted(result["_bif_frame_sort"].unique())
        for frame in frames:
            frame_objects = result[result["_bif_frame_sort"] == frame]
            used_tracks: set[int] = set()
            for index, obj in frame_objects.iterrows():
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
                assignments[index] = best_track

        result["track_id"] = [assignments[index] for index in result.index]
        result["track_count"] = len(set(assignments.values()))
        return result.drop(columns=["_bif_frame_sort", "_bif_label_sort"])


ultrack_env = EnvironmentSpec(
    name="tracking-ultrack",
    dependencies={
        "python": "3.12",
        "pip": ["ultrack", "numpy", "pandas"],
    },
)

btrack_env = EnvironmentSpec(
    name="tracking-btrack",
    dependencies={
        "python": "3.12",
        "pip": ["btrack", "numpy", "pandas"],
    },
)


class UltrackLink(LinkObjects):
    """Link objects with an Ultrack-compatible adapter."""

    display_name = "Ultrack Link"
    documentation = (
        "Link object tables with Ultrack when runtime='ultrack'. The default "
        "deterministic runtime uses the package nearest-neighbor linker for local tests."
    )
    tags = ["tracking", "ultrack", "linking"]
    environment = ultrack_env

    class Inputs(LinkObjects.Inputs):
        runtime: Annotated[
            str,
            GUIMeta(
                display_name="Runtime",
                description="'deterministic' for tests or 'ultrack' for the real runtime.",
            ),
        ] = "deterministic"

    def transform(self, df: Any, arguments: Any) -> Any:
        runtime = str(getattr(arguments, "runtime", "deterministic"))
        if runtime == "deterministic":
            return super().transform(df, arguments)
        if runtime != "ultrack":
            raise ValueError("runtime must be 'deterministic' or 'ultrack'.")
        ultrack = __import__("ultrack")
        if hasattr(ultrack, "link_objects"):
            return ultrack.link_objects(df, max_distance=arguments.max_distance)
        return ultrack.Linker(max_distance=arguments.max_distance).link(df)


class BTrackLink(LinkObjects):
    """Link objects with a btrack-compatible adapter."""

    display_name = "btrack Link"
    documentation = (
        "Link object tables with btrack when runtime='btrack'. The default "
        "deterministic runtime uses the package nearest-neighbor linker for local tests."
    )
    tags = ["tracking", "btrack", "linking"]
    environment = btrack_env

    class Inputs(LinkObjects.Inputs):
        runtime: Annotated[
            str,
            GUIMeta(
                display_name="Runtime",
                description="'deterministic' for tests or 'btrack' for the real runtime.",
            ),
        ] = "deterministic"

    def transform(self, df: Any, arguments: Any) -> Any:
        runtime = str(getattr(arguments, "runtime", "deterministic"))
        if runtime == "deterministic":
            return super().transform(df, arguments)
        if runtime != "btrack":
            raise ValueError("runtime must be 'deterministic' or 'btrack'.")
        btrack = __import__("btrack")
        if hasattr(btrack, "link_objects"):
            return btrack.link_objects(df, max_distance=arguments.max_distance)
        return btrack.Linker(max_distance=arguments.max_distance).link(df)
