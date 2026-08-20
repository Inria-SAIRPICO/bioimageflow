from pathlib import Path

import imageio.v3 as iio
import numpy as np
import pandas as pd
import pytest

from bioimageflow import Workflow
from bioimageflow.validation import serialize_output_schema
from bioimageflow_core import Arguments
from bioimageflow_common_tools import Files
from bioimageflow_tracking_tools import (
    FilterObjects,
    LabelsToObjects,
    NearestNeighborLink,
    TrackMetrics,
    TrackQualityMetrics,
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
    assert {Path(row.source_label_image) for row in result} == {label_path}
    assert {row.label for row in result} == {1, 2}
    assert {row.frame for row in result} == {0, 1, 2}


def test_labels_to_objects_all_background_returns_empty_object_table(
    tmp_path: Path,
) -> None:
    label_path = tmp_path / "background.tif"
    iio.imwrite(
        label_path, np.zeros((3, 16, 16), dtype=np.uint32), photometric="minisblack"
    )

    result = LabelsToObjects().process_row(Arguments(label_image=str(label_path)))

    assert result == []


def test_labels_to_objects_supports_sparse_uint32_ids(tmp_path: Path) -> None:
    label_path = tmp_path / "sparse_labels.tif"
    sparse_label = int(np.iinfo(np.uint32).max)
    labels = np.zeros((2, 5, 5), dtype=np.uint32)
    labels[0, 1:3, 1:3] = sparse_label
    labels[1, 2:5, 3:5] = sparse_label
    iio.imwrite(label_path, labels, photometric="minisblack")

    result = LabelsToObjects().process_row(Arguments(label_image=label_path))

    assert [row.label for row in result] == [sparse_label, sparse_label]
    assert [row.frame for row in result] == [0, 1]
    assert [row.area for row in result] == [4, 6]
    assert all(row.object_count == 2 for row in result)


@pytest.mark.parametrize(
    "labels, message",
    [
        (np.zeros((8, 8), dtype=np.float32), "integer label image"),
        (np.full((8, 8), -1, dtype=np.int16), "negative labels"),
    ],
)
def test_labels_to_objects_rejects_invalid_label_rasters(
    tmp_path: Path,
    labels: np.ndarray,
    message: str,
) -> None:
    label_path = tmp_path / "invalid.tif"
    iio.imwrite(label_path, labels, photometric="minisblack")

    with pytest.raises(ValueError, match=message):
        LabelsToObjects().process_row(Arguments(label_image=label_path))


def test_link_objects_and_track_metrics(tmp_path: Path) -> None:
    label_path = _moving_labels(tmp_path / "labels.tif")
    objects = LabelsToObjects().process_row(Arguments(label_image=str(label_path)))
    object_table = pd.DataFrame([vars(row) for row in objects])
    tracks = NearestNeighborLink().transform(
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


def test_nearest_neighbor_link_uses_global_one_to_one_assignment_without_area() -> None:
    objects = pd.DataFrame(
        [
            {"frame": 0, "label": 1, "y": 0.0, "x": 0.0},
            {"frame": 0, "label": 2, "y": 0.0, "x": 3.0},
            {"frame": 1, "label": 1, "y": 0.0, "x": 1.0},
            {"frame": 1, "label": 2, "y": 0.0, "x": -1.0},
        ]
    )

    result = NearestNeighborLink().transform(objects, Arguments(max_distance=3.0))

    assert result["track_id"].tolist() == [1, 2, 2, 1]
    assert result["track_count"].tolist() == [2, 2, 2, 2]


def test_nearest_neighbor_link_starts_new_tracks_after_a_frame_gap() -> None:
    objects = pd.DataFrame(
        [
            {"frame": 0, "label": 1, "y": 0.0, "x": 0.0},
            {"frame": 2, "label": 1, "y": 0.0, "x": 0.0},
        ]
    )

    result = NearestNeighborLink().transform(objects, Arguments(max_distance=1.0))

    assert result["track_id"].tolist() == [1, 2]


def test_nearest_neighbor_link_is_independent_of_index_and_preserves_columns() -> None:
    objects = pd.DataFrame(
        [
            {"frame": 1, "label": 1, "y": 0.0, "x": 1.0, "_bif_input_order": "a"},
            {"frame": 0, "label": 1, "y": 0.0, "x": 0.0, "_bif_input_order": "b"},
            {"frame": 1, "label": 2, "y": 10.0, "x": 10.0, "_bif_input_order": "c"},
        ],
        index=["duplicate", "duplicate", "duplicate"],
    )

    result = NearestNeighborLink().transform(objects, Arguments(max_distance=2.0))

    assert result.index.tolist() == objects.index.tolist()
    assert result["_bif_input_order"].tolist() == ["a", "b", "c"]
    assert result["track_id"].tolist() == [1, 1, 2]
    assert result["track_count"].tolist() == [2, 2, 2]


def test_nearest_neighbor_link_keeps_source_label_stacks_independent() -> None:
    objects = pd.DataFrame(
        [
            {
                "source_label_image": "first.tif",
                "frame": 0,
                "label": 1,
                "y": 0.0,
                "x": 0.0,
            },
            {
                "source_label_image": "first.tif",
                "frame": 1,
                "label": 1,
                "y": 1.0,
                "x": 0.0,
            },
            {
                "source_label_image": "second.tif",
                "frame": 0,
                "label": 1,
                "y": 10.0,
                "x": 0.0,
            },
            {
                "source_label_image": "second.tif",
                "frame": 1,
                "label": 1,
                "y": 11.0,
                "x": 0.0,
            },
        ]
    )

    result = NearestNeighborLink().transform(objects, Arguments(max_distance=2.0))

    assert result["track_id"].tolist() == [1, 1, 1, 1]
    assert result["track_count"].tolist() == [1, 1, 1, 1]


@pytest.mark.parametrize("column,value", [("frame", 0.5), ("label", 0), ("x", np.inf)])
def test_nearest_neighbor_link_rejects_invalid_values(
    column: str, value: float
) -> None:
    row = {"frame": 0, "label": 1, "y": 0.0, "x": 0.0}
    row[column] = value

    with pytest.raises(ValueError, match=column):
        NearestNeighborLink().transform(pd.DataFrame([row]), Arguments())


def test_nearest_neighbor_link_rejects_out_of_range_integer_columns() -> None:
    objects = pd.DataFrame(
        [{"frame": 0, "label": 10**30, "y": 0.0, "x": 0.0}]
    )

    with pytest.raises(ValueError, match="int64 range"):
        NearestNeighborLink().transform(objects, Arguments())


def test_removed_tracking_adapters_are_not_public_package_api() -> None:
    import bioimageflow_tracking_tools as tracking_tools

    assert "NearestNeighborLink" in tracking_tools.__all__
    assert "UltrackLink" not in tracking_tools.__all__
    assert "BTrackLink" not in tracking_tools.__all__
    assert "TrackSummary" not in tracking_tools.__all__


def test_empty_tracking_tables_keep_declared_contracts() -> None:
    objects = pd.DataFrame(
        columns=pd.Index(["frame", "label", "y", "x", "area", "object_count"])
    )

    tracks = NearestNeighborLink().transform(objects, Arguments(max_distance=5.0))
    summary = TrackMetrics().transform(tracks, Arguments())
    quality = TrackQualityMetrics().transform(tracks, Arguments(min_track_length=2))
    validation = TrackTableValidate().transform(tracks, Arguments())

    assert tracks.empty
    assert {"track_id", "track_count"} <= set(tracks.columns)
    assert summary.empty
    assert list(summary.columns) == [
        "source_label_image",
        "track_id",
        "track_length",
        "duration",
        "start_frame",
        "end_frame",
        "path_length",
        "net_displacement",
        "net_speed",
        "mean_step_speed",
        "mean_area",
        "track_count",
        "mean_track_length",
    ]
    assert quality.to_dict("records") == [
        {
            "source_label_image": None,
            "track_count": 0,
            "gap_count": 0,
            "duplicate_track_frame_count": 0,
            "object_assignment_conflict_count": 0,
            "short_track_fraction": 0.0,
        }
    ]
    assert validation.to_dict("records") == [
        {
            "source_label_image": None,
            "severity": "info",
            "message": "valid",
            "valid": True,
            "error_count": 0,
        }
    ]


def test_empty_source_keyed_table_keeps_quality_summary_contract() -> None:
    tracks = pd.DataFrame(
        columns=pd.Index(["source_label_image", "track_id", "frame", "label"])
    )

    result = TrackQualityMetrics().transform(tracks, Arguments())

    assert result.to_dict("records") == [
        {
            "source_label_image": None,
            "track_count": 0,
            "gap_count": 0,
            "duplicate_track_frame_count": 0,
            "object_assignment_conflict_count": 0,
            "short_track_fraction": 0.0,
        }
    ]


def test_tracking_workflow_graph_runs(tmp_path: Path) -> None:
    label_path = _moving_labels(tmp_path / "labels.tif")

    with Workflow(engine="direct", storage_path=str(tmp_path / "bif")) as wf:
        objects = LabelsToObjects()(label_image=label_path, name="objects")
        tracks = NearestNeighborLink()(
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


def test_filter_objects_requires_columns_for_requested_filters() -> None:
    with pytest.raises(ValueError, match="'intensity'"):
        FilterObjects().transform(
            pd.DataFrame([{"area": 4}]),
            Arguments(min_intensity=1.0),
        )


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


def test_tracks_to_labels_zero_tracks_preserves_artifact_and_count(
    tmp_path: Path,
) -> None:
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


def test_tracks_to_labels_coalesces_repeated_empty_anchors(tmp_path: Path) -> None:
    label_path = _moving_labels(tmp_path / "labels.tif")
    output = tmp_path / "track_labels.tif"

    results = TracksToLabels().process_batch(
        [
            Arguments(label_image=label_path, output_label_image=output),
            Arguments(label_image=label_path, output_label_image=output),
        ]
    )

    assert len(results) == 2
    assert len(results[0]) == 1
    assert results[1] == []
    assert Path(results[0][0].output_label_image) == output
    assert results[0][0].track_count == 0


def test_tracking_workflow_all_background_writes_zero_track_artifact(
    tmp_path: Path,
) -> None:
    label_path = tmp_path / "background.tif"
    source = np.zeros((3, 16, 16), dtype=np.uint32)
    iio.imwrite(label_path, source, photometric="minisblack")

    with Workflow(engine="direct", storage_path=str(tmp_path / "bif")) as wf:
        objects = LabelsToObjects()(label_image=label_path, name="objects")
        tracks = NearestNeighborLink()(objects, name="links")
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
        tracks = NearestNeighborLink()(objects, name="links")
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


def test_tracking_workflow_keeps_nonempty_sources_independent(tmp_path: Path) -> None:
    image_dir = tmp_path / "labels"
    image_dir.mkdir()
    for index, offset in enumerate((0, 10)):
        labels = np.zeros((2, 24, 24), dtype=np.uint16)
        labels[0, 2 + offset : 5 + offset, 2:5] = 1
        labels[1, 3 + offset : 6 + offset, 2:5] = 1
        iio.imwrite(
            image_dir / f"labels_{index}.tif",
            labels,
            photometric="minisblack",
        )

    with Workflow(engine="direct", storage_path=str(tmp_path / "bif")) as wf:
        files = Files()(path=image_dir, pattern="*.tif", name="files")
        objects = LabelsToObjects()(label_image=files["path"], name="objects")
        tracks = NearestNeighborLink()(objects, name="links")
        rendered = TracksToLabels()(
            track_id=tracks["track_id"],
            frame=tracks["frame"],
            label=tracks["label"],
            label_image=files["path"],
            name="render_tracks",
        )
        result = wf.compute(rendered)

    assert len(result) == 2
    assert result["track_count"].tolist() == [1, 1]
    for path in result["output_label_image"]:
        labels = iio.imread(path)
        assert set(np.unique(labels)) == {0, 1}


def test_tracking_workflow_writes_artifacts_for_mixed_empty_sources(
    tmp_path: Path,
) -> None:
    image_dir = tmp_path / "labels"
    image_dir.mkdir()
    populated = np.zeros((2, 16, 16), dtype=np.uint32)
    populated[0, 2:5, 2:5] = 7
    populated[1, 3:6, 2:5] = 9
    iio.imwrite(image_dir / "populated.tif", populated, photometric="minisblack")
    iio.imwrite(
        image_dir / "empty.tif",
        np.zeros_like(populated),
        photometric="minisblack",
    )

    with Workflow(engine="direct", storage_path=str(tmp_path / "bif")) as wf:
        files = Files()(path=image_dir, pattern="*.tif", name="files")
        objects = LabelsToObjects()(label_image=files["path"], name="objects")
        tracks = NearestNeighborLink()(objects, name="links")
        rendered = TracksToLabels()(
            track_id=tracks["track_id"],
            frame=tracks["frame"],
            label=tracks["label"],
            label_image=files["path"],
            name="render_tracks",
        )
        result = wf.compute(rendered)

    assert len(result) == 2
    by_name = {
        Path(path).name: (int(count), iio.imread(path))
        for path, count in zip(
            result["output_label_image"],
            result["track_count"],
            strict=True,
        )
    }
    empty_name = next(name for name in by_name if "empty" in name)
    populated_name = next(name for name in by_name if "populated" in name)
    assert by_name[empty_name][0] == 0
    assert int(by_name[empty_name][1].max()) == 0
    assert by_name[populated_name][0] == 1
    assert set(np.unique(by_name[populated_name][1])) == {0, 1}


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

    with pytest.raises(ValueError, match="positive|integer|<="):
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


@pytest.mark.parametrize(
    "frame,label,message",
    [(3, 1, "outside"), (0, 99, "does not contain")],
)
def test_tracks_to_labels_rejects_invalid_source_mappings(
    tmp_path: Path,
    frame: int,
    label: int,
    message: str,
) -> None:
    label_path = _moving_labels(tmp_path / "labels.tif")

    with pytest.raises(ValueError, match=message):
        TracksToLabels().process_batch(
            [
                Arguments(
                    track_id=1,
                    frame=frame,
                    label=label,
                    label_image=label_path,
                    output_label_image=tmp_path / "tracks.tif",
                )
            ]
        )


def test_tracks_to_labels_rejects_duplicate_object_assignments(tmp_path: Path) -> None:
    label_path = _moving_labels(tmp_path / "labels.tif")
    common = {
        "frame": 0,
        "label": 1,
        "label_image": label_path,
        "output_label_image": tmp_path / "tracks.tif",
    }

    with pytest.raises(ValueError, match="duplicate assignments"):
        TracksToLabels().process_batch(
            [Arguments(track_id=1, **common), Arguments(track_id=2, **common)]
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
    assert "column 'frame'" in result["message"].iloc[0]


def test_track_metrics_and_quality_report_motion_gaps_and_short_tracks() -> None:
    tracks = pd.DataFrame(
        [
            {"track_id": 1, "frame": 0, "label": 1, "y": 0.0, "x": 0.0, "area": 4},
            {"track_id": 1, "frame": 2, "label": 1, "y": 0.0, "x": 4.0, "area": 4},
            {"track_id": 2, "frame": 0, "label": 2, "y": 10.0, "x": 10.0, "area": 4},
        ]
    )

    summary = TrackMetrics().transform(tracks, Arguments())
    quality = TrackQualityMetrics().transform(
        tracks,
        Arguments(min_track_length=2),
    )

    assert summary["duration"].tolist() == [2, 0]
    assert summary["path_length"].round(2).tolist() == [4.0, 0.0]
    assert summary["net_displacement"].round(2).tolist() == [4.0, 0.0]
    assert summary["net_speed"].round(2).tolist() == [2.0, 0.0]
    assert summary["mean_step_speed"].round(2).tolist() == [2.0, 0.0]
    assert quality["gap_count"].iloc[0] == 1
    assert quality["short_track_fraction"].iloc[0] == 0.5
    assert quality["track_count"].iloc[0] == 2


def test_track_metrics_distinguishes_path_and_net_displacement() -> None:
    tracks = pd.DataFrame(
        [
            {"track_id": 1, "frame": 0, "y": 0.0, "x": 0.0, "area": 2},
            {"track_id": 1, "frame": 1, "y": 0.0, "x": 3.0, "area": 4},
            {"track_id": 1, "frame": 3, "y": 0.0, "x": 0.0, "area": 6},
        ]
    )

    result = TrackMetrics().transform(tracks, Arguments())

    assert result.iloc[0]["duration"] == 3
    assert result.iloc[0]["path_length"] == 6.0
    assert result.iloc[0]["net_displacement"] == 0.0
    assert result.iloc[0]["net_speed"] == 0.0
    assert result.iloc[0]["mean_step_speed"] == 2.25
    assert result.iloc[0]["mean_area"] == 4.0


def test_track_metrics_rejects_out_of_range_track_ids() -> None:
    tracks = pd.DataFrame(
        [{"track_id": 10**30, "frame": 0, "y": 0.0, "x": 0.0, "area": 1}]
    )

    with pytest.raises(ValueError, match="int64 range"):
        TrackMetrics().transform(tracks, Arguments())


def test_track_quality_names_duplicate_assignment_conflicts_truthfully() -> None:
    tracks = pd.DataFrame(
        [
            {"track_id": 1, "frame": 0, "label": 1},
            {"track_id": 1, "frame": 0, "label": 2},
            {"track_id": 2, "frame": 0, "label": 2},
        ]
    )

    result = TrackQualityMetrics().transform(tracks, Arguments(min_track_length=1))

    assert result.iloc[0]["duplicate_track_frame_count"] == 1
    assert result.iloc[0]["object_assignment_conflict_count"] == 1


def test_metrics_quality_and_validation_keep_source_stacks_independent() -> None:
    tracks = pd.DataFrame(
        [
            {
                "source_label_image": "first.tif",
                "track_id": 1,
                "frame": 0,
                "label": 1,
                "y": 0.0,
                "x": 0.0,
                "area": 4,
            },
            {
                "source_label_image": "second.tif",
                "track_id": 1,
                "frame": 0,
                "label": 1,
                "y": 10.0,
                "x": 10.0,
                "area": 9,
            },
        ]
    )

    metrics = TrackMetrics().transform(tracks, Arguments())
    quality = TrackQualityMetrics().transform(tracks, Arguments(min_track_length=1))
    validation = TrackTableValidate().transform(tracks, Arguments())

    assert metrics["source_label_image"].tolist() == ["first.tif", "second.tif"]
    assert metrics["track_count"].tolist() == [1, 1]
    assert metrics["mean_area"].tolist() == [4.0, 9.0]
    assert quality["source_label_image"].tolist() == ["first.tif", "second.tif"]
    assert quality["track_count"].tolist() == [1, 1]
    assert quality["duplicate_track_frame_count"].tolist() == [0, 0]
    assert quality["object_assignment_conflict_count"].tolist() == [0, 0]
    assert validation.to_dict("records") == [
        {
            "source_label_image": None,
            "severity": "info",
            "message": "valid",
            "valid": True,
            "error_count": 0,
        }
    ]
