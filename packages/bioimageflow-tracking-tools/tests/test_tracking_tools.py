from pathlib import Path
import sys
import types

import imageio.v3 as iio
import numpy as np
import pandas as pd
import pytest

from bioimageflow import Workflow
from bioimageflow.validation import serialize_input_schema, serialize_output_schema
from bioimageflow_core import Arguments
from bioimageflow_common_tools import Files
from bioimageflow_tracking_tools import (
    BTrackLink,
    FilterObjects,
    LabelsToObjects,
    TrackMetrics,
    TrackQualityMetrics,
    TrackSummary,
    TrackTableValidate,
    TracksToLabels,
    UltrackLink,
)
from bioimageflow_tracking_tools.linking import _NearestNeighborLinkObjects

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


def test_labels_to_objects_all_background_returns_empty_object_table(
    tmp_path: Path,
) -> None:
    label_path = tmp_path / "background.tif"
    iio.imwrite(label_path, np.zeros((3, 16, 16), dtype=np.uint32), photometric="minisblack")

    result = LabelsToObjects().process_row(Arguments(label_image=str(label_path)))

    assert result == []


def test_link_objects_and_track_metrics(tmp_path: Path) -> None:
    label_path = _moving_labels(tmp_path / "labels.tif")
    objects = LabelsToObjects().process_row(Arguments(label_image=str(label_path)))
    object_table = pd.DataFrame([vars(row) for row in objects])
    tracks = _NearestNeighborLinkObjects().transform(
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


def test_ultrack_and_btrack_adapter_schemas_do_not_expose_runtime_selectors() -> None:
    assert "runtime" not in serialize_input_schema(UltrackLink)
    assert "runtime" not in serialize_input_schema(BTrackLink)


def test_nearest_neighbor_linker_is_not_public_package_api() -> None:
    import bioimageflow_tracking_tools as tracking_tools

    assert "LinkObjects" not in tracking_tools.__all__
    assert not hasattr(tracking_tools, "LinkObjects")


def test_ultrack_and_btrack_adapters_dispatch_to_tracking_libraries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    table = pd.DataFrame(
        [{"frame": 0, "label": 1, "y": 1.0, "x": 2.0, "area": 4}]
    )

    ultrack_module = types.ModuleType("ultrack")
    btrack_module = types.ModuleType("btrack")

    def fake_ultrack_link(df: pd.DataFrame, *, max_distance: float) -> pd.DataFrame:
        result = df.copy()
        result["track_id"] = 10
        result["track_count"] = 1
        result["runtime"] = f"ultrack:{max_distance}"
        return result

    def fake_btrack_link(df: pd.DataFrame, *, max_distance: float) -> pd.DataFrame:
        result = df.copy()
        result["track_id"] = 20
        result["track_count"] = 1
        result["runtime"] = f"btrack:{max_distance}"
        return result

    ultrack_module.link_objects = fake_ultrack_link
    btrack_module.link_objects = fake_btrack_link
    monkeypatch.setitem(sys.modules, "ultrack", ultrack_module)
    monkeypatch.setitem(sys.modules, "btrack", btrack_module)

    ultrack_result = UltrackLink().transform(
        table,
        Arguments(max_distance=3.0),
    )
    btrack_result = BTrackLink().transform(
        table,
        Arguments(max_distance=4.0),
    )

    assert ultrack_result.iloc[0]["track_id"] == 10
    assert ultrack_result.iloc[0]["runtime"] == "ultrack:3.0"
    assert btrack_result.iloc[0]["track_id"] == 20
    assert btrack_result.iloc[0]["runtime"] == "btrack:4.0"


def test_empty_tracking_tables_keep_declared_contracts() -> None:
    objects = pd.DataFrame(
        columns=pd.Index(["frame", "label", "y", "x", "area", "object_count"])
    )

    tracks = _NearestNeighborLinkObjects().transform(objects, Arguments(max_distance=5.0))
    summary = TrackSummary().transform(tracks, Arguments())
    quality = TrackQualityMetrics().transform(tracks, Arguments(min_track_length=2))
    validation = TrackTableValidate().transform(tracks, Arguments())

    assert tracks.empty
    assert {"track_id", "track_count"} <= set(tracks.columns)
    assert summary.empty
    assert list(summary.columns) == [
        "track_id",
        "track_length",
        "duration",
        "start_frame",
        "end_frame",
        "displacement",
        "mean_speed",
        "track_count",
    ]
    assert quality.to_dict("records") == [
        {
            "track_count": 0,
            "gap_count": 0,
            "split_count": 0,
            "merge_count": 0,
            "short_track_fraction": 0.0,
        }
    ]
    assert validation.to_dict("records") == [
        {
            "severity": "info",
            "message": "valid",
            "valid": True,
            "error_count": 0,
        }
    ]


def test_tracking_workflow_graph_runs(tmp_path: Path) -> None:
    label_path = _moving_labels(tmp_path / "labels.tif")

    with Workflow(engine="direct", storage_path=str(tmp_path / "bif")) as wf:
        objects = LabelsToObjects()(label_image=label_path, name="objects")
        tracks = _NearestNeighborLinkObjects()(
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


def test_tracks_to_labels_zero_tracks_preserves_artifact_and_count(tmp_path: Path) -> None:
    label_path = _moving_labels(tmp_path / "labels.tif")

    result = TracksToLabels().process_batch(
        [
            Arguments(
                label_image=label_path,
                output_label_image=tmp_path / "track_labels.tif",
            ),
        ]
    )[0][0]

    labels = iio.imread(result.output_label_image)
    assert labels.dtype == np.uint32
    assert labels.shape == (3, 32, 32)
    assert int(labels.max()) == 0
    assert result.track_count == 0


def test_tracking_workflow_all_background_writes_zero_track_artifact(
    tmp_path: Path,
) -> None:
    label_path = tmp_path / "background.tif"
    source = np.zeros((3, 16, 16), dtype=np.uint32)
    iio.imwrite(label_path, source, photometric="minisblack")

    with Workflow(engine="direct", storage_path=str(tmp_path / "bif")) as wf:
        objects = LabelsToObjects()(label_image=label_path, name="objects")
        tracks = _NearestNeighborLinkObjects()(objects, name="links")
        rendered = TracksToLabels()(
            track_id=tracks["track_id"],
            frame=tracks["frame"],
            label=tracks["label"],
            label_image=label_path,
            name="render_tracks",
        )
        result = wf.compute(rendered)

    assert len(result) == 1
    assert int(result.iloc[0]["track_count"]) == 0
    labels = iio.imread(result.iloc[0]["output_label_image"])
    assert labels.dtype == np.uint32
    assert labels.shape == source.shape
    assert int(labels.max()) == 0


def test_tracking_workflow_all_background_writes_one_artifact_per_source(
    tmp_path: Path,
) -> None:
    image_dir = tmp_path / "labels"
    image_dir.mkdir()
    for index in range(2):
        iio.imwrite(
            image_dir / f"background_{index}.tif",
            np.zeros((3, 16, 16), dtype=np.uint32),
            photometric="minisblack",
        )

    with Workflow(engine="direct", storage_path=str(tmp_path / "bif")) as wf:
        files = Files()(path=image_dir, pattern="*.tif", name="files")
        objects = LabelsToObjects()(label_image=files["path"], name="objects")
        tracks = _NearestNeighborLinkObjects()(objects, name="links")
        rendered = TracksToLabels()(
            track_id=tracks["track_id"],
            frame=tracks["frame"],
            label=tracks["label"],
            label_image=files["path"],
            name="render_tracks",
        )
        result = wf.compute(rendered)

    assert len(result) == 2
    assert set(result["track_count"]) == {0}
    for path in result["output_label_image"]:
        labels = iio.imread(path)
        assert labels.dtype == np.uint32
        assert labels.shape == (3, 16, 16)
        assert int(labels.max()) == 0


def test_tracks_to_labels_output_schema_declares_uint32_labels() -> None:
    schema = serialize_output_schema(TracksToLabels)

    assert schema["output_label_image"]["image_spec"]["dtypes"] == ["uint32"]


def test_tracks_to_labels_preserves_track_ids_above_uint16(tmp_path: Path) -> None:
    label_path = _moving_labels(tmp_path / "labels.tif")
    large_track_id = int(np.iinfo(np.uint16).max) + 23

    result = TracksToLabels().process_batch(
        [
            Arguments(
                track_id=large_track_id,
                frame=0,
                label=1,
                label_image=label_path,
                output_label_image=tmp_path / "track_labels.tif",
            ),
        ]
    )[0][0]

    labels = iio.imread(result.output_label_image)
    assert labels.dtype == np.uint32
    assert int(labels[0, 6, 6]) == large_track_id
    assert result.track_count == 1


def test_tracks_to_labels_accepts_integer_like_track_ids(tmp_path: Path) -> None:
    label_path = _moving_labels(tmp_path / "labels.tif")

    result = TracksToLabels().process_batch(
        [
            Arguments(
                track_id="1.0",
                frame=0,
                label=1.0,
                label_image=label_path,
                output_label_image=tmp_path / "track_labels.tif",
            ),
        ]
    )[0][0]

    labels = iio.imread(result.output_label_image)
    assert labels.dtype == np.uint32
    assert int(labels[0, 6, 6]) == 1
    assert result.track_count == 1


@pytest.mark.parametrize(
    "track_id",
    [0, -1, 1.5, int(np.iinfo(np.uint32).max) + 1],
)
def test_tracks_to_labels_rejects_reserved_or_out_of_range_track_ids(
    tmp_path: Path,
    track_id: int | float,
) -> None:
    label_path = _moving_labels(tmp_path / "labels.tif")
    output_path = tmp_path / "track_labels.tif"

    with pytest.raises(ValueError, match="positive integer|<="):
        TracksToLabels().process_batch(
            [
                Arguments(
                    track_id=track_id,
                    frame=0,
                    label=1,
                    label_image=label_path,
                    output_label_image=output_path,
                ),
            ]
        )

    assert not output_path.exists()


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
