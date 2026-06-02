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
    TrackQualityMetrics,
    TrackSummary,
    TrackTableValidate,
    TrackMetrics,
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


def test_filter_objects_keeps_rows_within_area_frame_and_position(tmp_path: Path) -> None:
    objects_csv = tmp_path / "objects.csv"
    pd.DataFrame(
        [
            {"frame": 0, "label": 1, "y": 4.0, "x": 4.0, "area": 4},
            {"frame": 1, "label": 2, "y": 12.0, "x": 12.0, "area": 20},
            {"frame": 2, "label": 3, "y": 25.0, "x": 25.0, "area": 12},
        ]
    ).to_csv(objects_csv, index=False)

    result = FilterObjects().process_row(
        Arguments(
            objects_csv=objects_csv,
            min_area=10.0,
            max_area=15.0,
            min_frame=1,
            max_frame=2,
            max_y=20.0,
            filtered_objects_csv=tmp_path / "filtered.csv",
        )
    )

    table = pd.read_csv(result.filtered_objects_csv)
    assert table["label"].tolist() == []
    assert result.object_count == 0


def test_tracks_to_labels_renders_track_ids_into_label_stack(tmp_path: Path) -> None:
    label_path = _moving_labels(tmp_path / "labels.tif")
    tracks_csv = tmp_path / "tracks.csv"
    pd.DataFrame(
        [
            {"track_id": 5, "frame": 0, "label": 1, "y": 6.5, "x": 6.5, "area": 16},
            {"track_id": 7, "frame": 0, "label": 2, "y": 21.5, "x": 23.5, "area": 16},
        ]
    ).to_csv(tracks_csv, index=False)

    result = TracksToLabels().process_row(
        Arguments(
            tracks_csv=tracks_csv,
            label_image=label_path,
            output_label_image=tmp_path / "track_labels.tif",
        )
    )

    labels = iio.imread(result.output_label_image)
    assert labels[0, 6, 6] == 5
    assert labels[0, 21, 23] == 7
    assert result.track_count == 2


def test_tracks_to_labels_rejects_missing_required_numeric_fields(
    tmp_path: Path,
) -> None:
    label_path = _moving_labels(tmp_path / "labels.tif")
    tracks_csv = tmp_path / "bad_tracks.csv"
    pd.DataFrame([{"track_id": 5, "frame": 0, "y": 6.5, "x": 6.5}]).to_csv(
        tracks_csv,
        index=False,
    )

    with pytest.raises(ValueError, match="required column 'label'"):
        TracksToLabels().process_row(
            Arguments(
                tracks_csv=tracks_csv,
                label_image=label_path,
                output_label_image=tmp_path / "track_labels.tif",
            )
        )


def test_track_table_validate_flags_duplicate_track_frames(tmp_path: Path) -> None:
    tracks_csv = tmp_path / "bad_tracks.csv"
    pd.DataFrame(
        [
            {"track_id": 1, "frame": 0, "label": 1, "y": 0.0, "x": 0.0, "area": 4},
            {"track_id": 1, "frame": 0, "label": 2, "y": 1.0, "x": 1.0, "area": 4},
        ]
    ).to_csv(tracks_csv, index=False)

    result = TrackTableValidate().process_row(
        Arguments(tracks_csv=tracks_csv, validation_csv=tmp_path / "validation.csv")
    )

    validation = pd.read_csv(result.validation_csv)
    assert result.valid is False
    assert result.error_count == 1
    assert "duplicate" in validation.loc[0, "message"]


def test_track_table_validate_flags_blank_required_values(tmp_path: Path) -> None:
    tracks_csv = tmp_path / "bad_tracks.csv"
    pd.DataFrame(
        [{"track_id": 1, "frame": "", "label": 2, "y": 1.0, "x": 1.0, "area": 4}]
    ).to_csv(tracks_csv, index=False)

    result = TrackTableValidate().process_row(
        Arguments(tracks_csv=tracks_csv, validation_csv=tmp_path / "validation.csv")
    )

    validation = pd.read_csv(result.validation_csv)
    assert result.valid is False
    assert result.error_count == 1
    assert "required column 'frame'" in validation.loc[0, "message"]


def test_track_summary_and_quality_metrics_report_gaps_and_short_tracks(
    tmp_path: Path,
) -> None:
    tracks_csv = tmp_path / "tracks.csv"
    pd.DataFrame(
        [
            {"track_id": 1, "frame": 0, "label": 1, "y": 0.0, "x": 0.0, "area": 4},
            {"track_id": 1, "frame": 2, "label": 1, "y": 0.0, "x": 4.0, "area": 4},
            {"track_id": 2, "frame": 0, "label": 2, "y": 10.0, "x": 10.0, "area": 4},
        ]
    ).to_csv(tracks_csv, index=False)

    summary = TrackSummary().process_row(
        Arguments(tracks_csv=tracks_csv, summary_csv=tmp_path / "summary.csv")
    )
    quality = TrackQualityMetrics().process_row(
        Arguments(
            tracks_csv=tracks_csv,
            min_track_length=2,
            quality_csv=tmp_path / "quality.csv",
        )
    )

    summary_table = pd.read_csv(summary.summary_csv).sort_values("track_id")
    quality_table = pd.read_csv(quality.quality_csv)
    assert summary_table["duration"].tolist() == [3, 1]
    assert summary_table["displacement"].round(2).tolist() == [4.0, 0.0]
    assert quality.gap_count == 1
    assert quality.short_track_fraction == 0.5
    assert quality_table.loc[0, "track_count"] == 2
