"""Live-cell migration tracking workflow with Ultrack and btrack adapters."""

import argparse
from pathlib import Path
from typing import Annotated, Any

import pandas as pd

from bioimageflow import DataFrameTool, Workflow
from bioimageflow_core import Category, GUIMeta, IOModel
from bioimageflow_common_tools import Concat
from bioimageflow_tracking_tools import BTrackLink, LabelsToObjects, TrackMetrics, UltrackLink

DEFAULT_STORAGE_PATH = Path(__file__).resolve().parent / "results"


class AddTrackerName(DataFrameTool):
    """Attach the tracker name to a metrics table."""

    display_name = "Add Tracker Name"
    category = Category.UTILITIES

    class Inputs(IOModel):
        tracker: Annotated[str, GUIMeta(display_name="Tracker")]

    class Outputs(IOModel):
        tracker: str

    def transform(self, df: Any, arguments: Any) -> pd.DataFrame:
        table = pd.DataFrame(df).copy()
        table.insert(0, "tracker", str(arguments.tracker))
        return table


def build_workflow(
    *,
    storage_path: str | Path = DEFAULT_STORAGE_PATH,
    engine: str = "wetlands",
    wetlands_config: dict | None = None,
) -> Workflow:
    """Build an Ultrack/btrack migration metrics workflow without lineage outputs."""
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
        ultrack_tracks = UltrackLink()(objects, name="ultrack_tracks")
        btrack_tracks = BTrackLink()(objects, name="btrack_tracks")
        ultrack_metrics = TrackMetrics()(ultrack_tracks, name="ultrack_migration_metrics")
        btrack_metrics = TrackMetrics()(btrack_tracks, name="btrack_migration_metrics")
        tagged_ultrack = AddTrackerName()(
            ultrack_metrics,
            tracker="ultrack",
            name="tag_ultrack_metrics",
        )
        tagged_btrack = AddTrackerName()(
            btrack_metrics,
            tracker="btrack",
            name="tag_btrack_metrics",
        )
        summary = Concat()(tagged_ultrack, tagged_btrack, name="migration_metrics")
        wf.output("tracker", summary["tracker"], id="output-tracker")
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
