from pathlib import Path

import imageio.v3 as iio
import numpy as np
import pandas as pd

from bioimageflow import Workflow
from bioimageflow_core import Arguments
from bioimageflow_tracking_tools import LabelsToObjects, LinkObjects, TrackMetrics


def _moving_labels(path: Path) -> Path:
    labels = np.zeros((3, 32, 32), dtype=np.uint16)
    labels[0, 5:9, 5:9] = 1
    labels[1, 6:10, 7:11] = 1
    labels[2, 7:11, 9:13] = 1
    labels[0, 20:24, 22:26] = 2
    labels[1, 19:23, 20:24] = 2
    labels[2, 18:22, 18:22] = 2
    iio.imwrite(path, labels, photometric="minisblack")
    return path


def test_labels_to_objects_extracts_centroids(tmp_path: Path) -> None:
    label_path = _moving_labels(tmp_path / "labels.tif")

    result = LabelsToObjects().process_row(
        Arguments(label_image=str(label_path), objects_csv=str(tmp_path / "objects.csv"))
    )

    objects = pd.read_csv(result.objects_csv)
    assert len(objects) == 6
    assert set(objects.columns) == {"frame", "label", "y", "x", "area"}
    assert result.object_count == 6


def test_link_objects_and_track_metrics(tmp_path: Path) -> None:
    label_path = _moving_labels(tmp_path / "labels.tif")
    objects = LabelsToObjects().process_row(
        Arguments(label_image=str(label_path), objects_csv=str(tmp_path / "objects.csv"))
    )
    tracks = LinkObjects().process_row(
        Arguments(
            objects_csv=str(objects.objects_csv),
            tracks_csv=str(tmp_path / "tracks.csv"),
            max_distance=5.0,
        )
    )
    metrics = TrackMetrics().process_row(
        Arguments(
            tracks_csv=str(tracks.tracks_csv),
            metrics_csv=str(tmp_path / "metrics.csv"),
        )
    )

    track_table = pd.read_csv(tracks.tracks_csv)
    assert track_table["track_id"].nunique() == 2
    assert tracks.track_count == 2
    metrics_table = pd.read_csv(metrics.metrics_csv)
    assert metrics_table["track_length"].tolist() == [3, 3]
    assert metrics.mean_track_length == 3.0


def test_tracking_workflow_graph_runs(tmp_path: Path) -> None:
    label_path = _moving_labels(tmp_path / "labels.tif")

    with Workflow(storage_path=str(tmp_path / "bif")) as wf:
        objects = LabelsToObjects()(label_image=label_path, name="objects")
        tracks = LinkObjects()(objects_csv=objects["objects_csv"], name="links")
        metrics = TrackMetrics()(tracks_csv=tracks["tracks_csv"], name="metrics")
        result = wf.compute(metrics)

    assert Path(result.iloc[0]["metrics_csv"]).exists()
