from pathlib import Path

import imageio.v3 as iio
import numpy as np
import pandas as pd
import pytest

from bioimageflow import Workflow
from bioimageflow_core import Arguments
from bioimageflow_tracking_tools import (
    FilterObjects,
    LabelsToObjects,
    LinkObjects,
    TrackMetrics,
    TrackQualityMetrics,
    TrackSummary,
    TrackTableValidate,
    TracksToLabels,
)

pytestmark = pytest.mark.package_tools


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

    result = LabelsToObjects().process_row(Arguments(label_image=str(label_path)))

    assert len(result) == 6
    assert result[0].object_count == 6
    assert {row.label for row in result} == {1, 2}
    assert {row.frame for row in result} == {0, 1, 2}


def test_link_objects_and_track_metrics(tmp_path: Path) -> None:
    label_path = _moving_labels(tmp_path / "labels.tif")
    objects = LabelsToObjects().process_row(Arguments(label_image=str(label_path)))
    object_table = pd.DataFrame([vars(row) for row in objects])
    tracks = LinkObjects().transform(
        object_table,
        Arguments(max_distance=5.0),
    )
    metrics = TrackMetrics().transform(
        tracks,
        Arguments(),
    )

    assert set(tracks["track_id"]) == {1, 2}
    assert tracks["track_count"].iloc[0] == 2
    assert metrics["track_length"].tolist() == [3, 3]
    assert metrics["mean_track_length"].iloc[0] == 3.0


def test_tracking_workflow_graph_runs(tmp_path: Path) -> None:
    label_path = _moving_labels(tmp_path / "labels.tif")

    with Workflow(storage_path=str(tmp_path / "bif")) as wf:
        objects = LabelsToObjects()(label_image=label_path, name="objects")
        tracks = LinkObjects()(
            objects,
            name="links",
        )
        metrics = TrackMetrics()(
            tracks,
            name="metrics",
        )
        result = wf.compute(metrics)

    assert len(result) == 2
    assert result["track_length"].tolist() == [3, 3]


def test_filter_objects_keeps_rows_within_area_frame_and_position() -> None:
    table = pd.DataFrame(
        [
            {"frame": 0, "label": 1, "y": 4.0, "x": 4.0, "area": 4, "source": "a"},
            {"frame": 1, "label": 2, "y": 12.0, "x": 12.0, "area": 20, "source": "b"},
            {"frame": 2, "label": 3, "y": 25.0, "x": 25.0, "area": 12, "source": "c"},
        ]
    )

    result = FilterObjects().transform(
        table,
        Arguments(
            min_area=10.0,
            max_area=15.0,
            min_frame=1,
            max_frame=2,
            max_y=20.0,
        ),
    )

    assert result.empty
    assert "source" in result.columns
    assert "object_count" in result.columns


def test_tracks_to_labels_renders_track_ids_into_label_stack(tmp_path: Path) -> None:
    label_path = _moving_labels(tmp_path / "labels.tif")

    result = TracksToLabels().process_batch(
        [
            Arguments(
                track_id=5,
                frame=0,
                label=1,
                label_image=label_path,
                output_label_image=tmp_path / "track_labels.tif",
            ),
            Arguments(
                track_id=7,
                frame=0,
                label=2,
                label_image=label_path,
                output_label_image=tmp_path / "track_labels.tif",
            ),
        ]
    )[0][0]

    labels = iio.imread(result.output_label_image)
    assert labels[0, 6, 6] == 5
    assert labels[0, 21, 23] == 7
    assert result.track_count == 2


def test_tracks_to_labels_rejects_missing_required_numeric_fields(
    tmp_path: Path,
) -> None:
    label_path = _moving_labels(tmp_path / "labels.tif")

    with pytest.raises(ValueError, match="required column 'label'"):
        TracksToLabels().process_batch(
            [
                Arguments(
                    track_id=5,
                    frame=0,
                    label_image=label_path,
                    output_label_image=tmp_path / "track_labels.tif",
                )
            ]
        )


def test_track_table_validate_flags_duplicate_track_frames() -> None:
    result = TrackTableValidate().transform(
        pd.DataFrame(
            [
                {"track_id": 1, "frame": 0, "label": 1, "y": 0.0, "x": 0.0},
                {"track_id": 1, "frame": 0, "label": 2, "y": 1.0, "x": 1.0},
            ]
        ),
        Arguments(),
    )

    assert bool(result["valid"].iloc[0]) is False
    assert result["error_count"].iloc[0] == 1
    assert "duplicate" in result["message"].iloc[0]


def test_track_table_validate_flags_blank_required_values() -> None:
    result = TrackTableValidate().transform(
        pd.DataFrame([{"track_id": 1, "frame": "", "label": 2, "y": 1.0, "x": 1.0}]),
        Arguments(),
    )

    assert bool(result["valid"].iloc[0]) is False
    assert result["error_count"].iloc[0] == 1
    assert "required column 'frame'" in result["message"].iloc[0]


def test_track_summary_and_quality_metrics_report_gaps_and_short_tracks() -> None:
    tracks = pd.DataFrame(
        [
            {"track_id": 1, "frame": 0, "label": 1, "y": 0.0, "x": 0.0, "area": 4},
            {"track_id": 1, "frame": 2, "label": 1, "y": 0.0, "x": 4.0, "area": 4},
            {"track_id": 2, "frame": 0, "label": 2, "y": 10.0, "x": 10.0, "area": 4},
        ]
    )

    summary = TrackSummary().transform(tracks, Arguments())
    quality = TrackQualityMetrics().transform(
        tracks,
        Arguments(min_track_length=2),
    )

    assert summary["duration"].tolist() == [3, 1]
    assert summary["displacement"].round(2).tolist() == [4.0, 0.0]
    assert quality["gap_count"].iloc[0] == 1
    assert quality["short_track_fraction"].iloc[0] == 0.5
    assert quality["track_count"].iloc[0] == 2
