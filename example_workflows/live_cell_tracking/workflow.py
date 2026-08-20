"""Live-cell migration tracking with deterministic nearest-neighbor linking."""

import argparse
from pathlib import Path
from bioimageflow import Workflow
from bioimageflow_common_tools import CrossJoin, SelectColumns
from bioimageflow_tracking_tools import (
    LabelsToObjects,
    NearestNeighborLink,
    TrackMetrics,
    TrackQualityMetrics,
)

DEFAULT_STORAGE_PATH = Path(__file__).resolve().parent / "results"


def build_workflow(
    *,
    storage_path: str | Path = DEFAULT_STORAGE_PATH,
    engine: str = "wetlands",
    wetlands_config: dict | None = None,
) -> Workflow:
    """Build a deterministic migration and track-quality workflow."""
    storage = Path(storage_path)
    wf = Workflow(
        name="live_cell_tracking",
        display_name="Live Cell Tracking",
        storage_path=str(storage),
        engine=engine,
        wetlands_config=wetlands_config,
    )
    with wf:
        label_image = wf.input("label_image", Path, id="input-label-image")
        objects = LabelsToObjects()(label_image=label_image, name="objects")
        tracks = NearestNeighborLink()(
            objects,
            max_distance=10.0,
            name="nearest_neighbor_tracks",
        )
        metrics = TrackMetrics()(tracks, name="migration_metrics_by_track")
        quality = TrackQualityMetrics()(
            tracks,
            min_track_length=3,
            name="tracking_quality",
        )
        quality_fields = SelectColumns()(
            quality,
            columns=(
                "gap_count,duplicate_track_frame_count,"
                "object_assignment_conflict_count,short_track_fraction"
            ),
            name="quality_fields",
        )
        summary = CrossJoin()(metrics, quality_fields, name="migration_metrics")
        for output_name in (
            "track_id",
            "track_length",
            "duration",
            "path_length",
            "net_displacement",
            "net_speed",
            "mean_step_speed",
            "mean_area",
            "track_count",
            "mean_track_length",
            "gap_count",
            "duplicate_track_frame_count",
            "object_assignment_conflict_count",
            "short_track_fraction",
        ):
            wf.output(output_name, summary[output_name], id=f"output-{output_name.replace('_', '-')}")
    return wf


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--label-image", required=True, help="TYX label movie or CTC-style label stack.")
    parser.add_argument(
        "--storage-path",
        default=str(DEFAULT_STORAGE_PATH),
        help="Directory for workflow outputs.",
    )
    args = parser.parse_args()
    workflow = build_workflow(storage_path=args.storage_path)
    print(workflow.compute(inputs={"label_image": args.label_image}).to_string(index=False))
