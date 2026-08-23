"""Lineage-aware cell tracking with LapTrack and label rendering."""

import argparse
from pathlib import Path

from bioimageflow import Workflow
from bioimageflow_tracking_tools import (
    LabelsToObjects,
    LapTrackLink,
    TrackMetrics,
    TrackQualityMetrics,
    TrackTableValidate,
    TracksToLabels,
)


DEFAULT_STORAGE_PATH = Path(__file__).resolve().parent / "results"


def build_workflow(
    *,
    storage_path: str | Path = DEFAULT_STORAGE_PATH,
    engine: str = "wetlands",
    wetlands_config: dict | None = None,
) -> Workflow:
    """Build a divisions-without-merges lineage workflow."""
    wf = Workflow(
        name="cell_lineage_tracking",
        display_name="Cell Lineage Tracking",
        storage_path=str(storage_path),
        engine=engine,
        wetlands_config=wetlands_config,
    )
    with wf:
        labels = wf.input("label_image", Path, id="input-label-image")
        objects = LabelsToObjects()(label_image=labels, name="objects_from_labels")
        tracks = LapTrackLink()(
            source_label_image=objects["source_label_image"],
            frame=objects["frame"],
            label=objects["label"],
            y=objects["y"],
            x=objects["x"],
            area=objects["area"],
            max_link_distance=15.0,
            gap_closing_distance=30.0,
            gap_closing_max_frames=2,
            division_distance=30.0,
            name="laptrack_lineages",
        )
        TrackTableValidate()(tracks, name="validate_track_table")
        TrackMetrics()(tracks, name="lineage_migration_metrics")
        TrackQualityMetrics()(tracks, min_track_length=3, name="lineage_quality")
        rendered = TracksToLabels()(
            track_id=tracks["track_id"],
            frame=tracks["frame"],
            label=tracks["label"],
            label_image=tracks["source_label_image"],
            name="render_lineage_labels",
        )
        wf.output("track_id", tracks["track_id"], id="output-track-id")
        wf.output("lineage_id", tracks["lineage_id"], id="output-lineage-id")
        wf.output("parent_track_id", tracks["parent_track_id"], id="output-parent-track-id")
        wf.output("generation", tracks["generation"], id="output-generation")
        wf.output("track_labels", rendered["output_label_image"], id="output-track-labels")
    return wf


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--label-image", required=True)
    parser.add_argument("--storage-path", default=str(DEFAULT_STORAGE_PATH))
    args = parser.parse_args()
    workflow = build_workflow(storage_path=args.storage_path)
    print(
        workflow.compute(inputs={"label_image": args.label_image}).to_string(index=False)
    )
