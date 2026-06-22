"""Live-cell migration tracking workflow with Ultrack and btrack adapters."""

from pathlib import Path
from typing import Annotated, Any

import imageio.v3 as iio
import numpy as np
import pandas as pd

from bioimageflow import DataFrameTool, Workflow
from bioimageflow.node import Node
from bioimageflow_core import Arguments, Category, GENERAL_ENV, GUIMeta, IOModel, ProcessingTool
from bioimageflow_common_tools import Concat
from bioimageflow_tracking_tools import BTrackLink, LabelsToObjects, TrackMetrics, UltrackLink


class TrackingFixture(ProcessingTool):
    """Write a tiny TYX label movie with two migrating objects."""

    display_name = "Tracking Fixture"
    category = Category.UTILITIES
    environment = GENERAL_ENV

    class Inputs(IOModel):
        output_dir: Annotated[Path, GUIMeta(display_name="Output directory")]

    class Outputs(IOModel):
        label_image: Annotated[Path, GUIMeta(display_name="Label movie")]

    def process_row(self, arguments: Arguments, *, context: Any = None) -> Any:
        output_dir = Path(arguments.output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        labels = np.zeros((4, 40, 40), dtype=np.uint16)
        for frame in range(labels.shape[0]):
            labels[frame, 8 + frame : 13 + frame, 7 + 2 * frame : 12 + 2 * frame] = 1
            labels[frame, 27 - frame : 32 - frame, 28 - frame : 33 - frame] = 2
        path = output_dir / "ctc_tiny_live_cell_labels.tif"
        iio.imwrite(path, labels, photometric="minisblack")
        return self.Outputs(label_image=path)


class AddTrackerName(DataFrameTool):
    """Attach the tracker name to a metrics table."""

    display_name = "Add Tracker Name"
    category = Category.UTILITIES

    class Inputs(IOModel):
        tracker: Annotated[str, GUIMeta(display_name="Tracker")]

    class Outputs(IOModel):
        pass

    def transform(self, df: Any, arguments: Any) -> pd.DataFrame:
        table = pd.DataFrame(df).copy()
        table.insert(0, "tracker", str(arguments.tracker))
        return table


def build_workflow(
    storage_path: str = "./live_cell_tracking_results",
    engine: str = "wetlands",
    wetlands_config: dict | None = None,
) -> tuple[Workflow, Node]:
    """Build an Ultrack/btrack migration metrics workflow without lineage outputs."""
    storage = Path(storage_path)
    wf = Workflow(
        storage_path=str(storage / "bif"),
        engine=engine,
        wetlands_config=wetlands_config,
    )
    with wf:
        fixture = TrackingFixture()(output_dir=storage / "data", name="tracking_fixture")
        objects = LabelsToObjects()(label_image=fixture["label_image"], name="objects")
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
    return wf, summary


if __name__ == "__main__":
    workflow, terminal = build_workflow()
    print(workflow.compute(terminal).to_string(index=False))
