"""Synthetic label-to-track workflow."""

from pathlib import Path

import imageio.v3 as iio
import numpy as np

from bioimageflow import Workflow
from bioimageflow.node import Node
from bioimageflow_tracking_tools import LabelsToObjects, LinkObjects, TrackMetrics


def _write_labels(data_dir: Path) -> Path:
    data_dir.mkdir(parents=True, exist_ok=True)
    labels = np.zeros((4, 48, 48), dtype=np.uint16)
    for frame in range(labels.shape[0]):
        labels[frame, 8 + frame : 13 + frame, 10 + frame : 15 + frame] = 1
        labels[frame, 30 - frame : 35 - frame, 31 - frame : 36 - frame] = 2
    label_path = data_dir / "synthetic_tracks.tif"
    iio.imwrite(label_path, labels, photometric="minisblack")
    return label_path


def build_workflow(
    storage_path: str = "./tracking_analysis_results",
    engine: str = "direct",
    wetlands_config: dict | None = None,
) -> tuple[Workflow, Node]:
    """Build the tracking workflow."""
    storage = Path(storage_path)
    label_path = _write_labels(storage / "data")
    wf = Workflow(
        storage_path=str(storage / "bif"),
        engine=engine,
        wetlands_config=wetlands_config,
    )
    with wf:
        objects = LabelsToObjects()(label_image=label_path, name="labels_to_objects")
        tracks = LinkObjects()(
            objects,
            max_distance=8.0,
            name="link_objects",
        )
        metrics = TrackMetrics()(
            tracks,
            name="track_metrics",
        )
    return wf, metrics


if __name__ == "__main__":
    workflow, terminal = build_workflow()
    print(workflow.compute(terminal).to_string(index=False))
