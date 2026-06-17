import json
from pathlib import Path

import imageio.v3 as iio
import numpy as np
import pandas as pd
import pytest

from bioimageflow import Workflow
from bioimageflow.validation import serialize_output_schema
from bioimageflow_core import Arguments
from bioimageflow_common_tools import Files
import bioimageflow_spot_tools.detection as spot_detection
from bioimageflow_spot_tools import (
    AssignSpotsToLabels,
    DetectSpots,
    FilterSpots,
    RenderSpots,
    SpotColocalization,
    SpotQualityMetrics,
    SpotSummary,
    SpotsToLabels,
)

pytestmark = pytest.mark.package_tools


def _index(values: list[str]) -> pd.Index:
    return pd.Index(values)


def _spot_image(path: Path) -> Path:
    image = np.zeros((48, 48), dtype=np.float32)
    for y, x, value in [(12, 10, 12.0), (18, 35, 9.0), (34, 24, 11.0)]:
        image[y, x] = value
        image[y - 1 : y + 2, x - 1 : x + 2] += value * 0.25
    iio.imwrite(path, image)
    return path


def test_detect_spots_finds_synthetic_puncta(tmp_path: Path) -> None:
    input_image = _spot_image(tmp_path / "puncta.tif")

    result = DetectSpots().process_row(
        Arguments(
            input_image=str(input_image),
            method="dog",
            sigma=1.0,
            sigma_ratio=1.6,
            threshold=0.3,
            min_distance=5,
            output_labels=str(tmp_path / "spots.tif"),
        )
    )

    assert len(result) == 3
    assert result[0].spot_count == 3
    labels = iio.imread(result[0].output_labels)
    assert labels.max() == 3
    assert {spot.spot_id for spot in result} == {1, 2, 3}


def test_spot_label_output_schemas_declare_uint32_label_contract() -> None:
    detect_schema = serialize_output_schema(DetectSpots)
    render_schema = serialize_output_schema(RenderSpots)
    labels_schema = serialize_output_schema(SpotsToLabels)

    assert detect_schema["output_labels"]["image_spec"]["dtypes"] == ["uint32"]
    assert render_schema["output_image"]["image_spec"]["dtypes"] == ["uint32", "uint8"]
    assert render_schema["output_image"]["image_spec"]["semantics"] == ["binary", "label"]
    assert labels_schema["label_image"]["image_spec"]["dtypes"] == ["uint32"]


def test_detect_spots_writes_uint32_labels_for_large_spot_counts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    input_image = tmp_path / "puncta.tif"
    iio.imwrite(input_image, np.ones((2, 2), dtype=np.float32))
    large_spot_id = int(np.iinfo(np.uint16).max) + 1
    monkeypatch.setattr(
        spot_detection,
        "_local_maxima",
        lambda *_args, **_kwargs: [(0, 0)] * large_spot_id,
    )

    result = DetectSpots().process_row(
        Arguments(
            input_image=str(input_image),
            method="local_maxima",
            sigma=1.0,
            sigma_ratio=1.6,
            threshold=0.0,
            min_distance=1,
            output_labels=str(tmp_path / "spots.tif"),
        )
    )

    labels = iio.imread(result[0].output_labels)
    assert labels.dtype == np.uint32
    assert int(labels[0, 0]) == large_spot_id
    assert result[0].spot_count == large_spot_id


def test_detect_spots_log_method_finds_synthetic_puncta(tmp_path: Path) -> None:
    input_image = _spot_image(tmp_path / "puncta.tif")

    result = DetectSpots().process_row(
        Arguments(
            input_image=str(input_image),
            method="log",
            sigma=1.0,
            sigma_ratio=1.6,
            threshold=0.2,
            min_distance=5,
            output_labels=str(tmp_path / "log_spots.tif"),
        )
    )

    assert len(result) == 3
    assert all(spot.score > 0 for spot in result)


def test_assign_spots_to_labels_and_summarize(tmp_path: Path) -> None:
    spots = [
        Arguments(spot_id=1, y=6, x=6, intensity=10.0, score=4.0),
        Arguments(spot_id=2, y=10, x=10, intensity=12.0, score=5.0),
        Arguments(spot_id=3, y=30, x=30, intensity=8.0, score=3.0),
    ]
    labels = np.zeros((40, 40), dtype=np.uint16)
    labels[0:20, 0:20] = 1
    labels[25:35, 25:35] = 2
    label_path = tmp_path / "labels.tif"
    iio.imwrite(label_path, labels)

    assigned = [
        AssignSpotsToLabels().process_row(
            Arguments(**vars(spot), label_image=str(label_path))
        )
        for spot in spots
    ]
    assigned_table = pd.DataFrame(
        [
            {"label": row.label, "intensity": row.intensity}
            for row in assigned
        ],
        index=_index([f"spot::{index}" for index in range(len(assigned))]),
    )
    summary = SpotSummary().transform(
        assigned_table,
        Arguments(),
    )

    assert [row.label for row in assigned] == [1, 1, 2]
    assert list(summary["spot_count"]) == [2, 1]
    assert list(summary["total_intensity"]) == [22.0, 8.0]


def test_spot_tools_build_workflow_graph(tmp_path: Path) -> None:
    image = _spot_image(tmp_path / "puncta.tif")
    labels = np.ones((48, 48), dtype=np.uint16)
    labels_path = tmp_path / "labels.tif"
    iio.imwrite(labels_path, labels)

    with Workflow(storage_path=str(tmp_path / "bif")) as wf:
        detected = DetectSpots()(input_image=image, threshold=0.3, name="detect")
        assigned = AssignSpotsToLabels()(
            spot_id=detected["spot_id"],
            y=detected["y"],
            x=detected["x"],
            intensity=detected["intensity"],
            score=detected["score"],
            label_image=labels_path,
            name="assign",
        )
        summary = SpotSummary()(assigned, name="summary")
        result = wf.compute(summary)

    assert len(result) == 1
    assert result.iloc[0]["spot_count"] == 3
    assert result.iloc[0]["total_intensity"] > 0


def test_zero_spot_workflow_keeps_downstream_spot_table_empty(tmp_path: Path) -> None:
    image = tmp_path / "blank.tif"
    labels = tmp_path / "labels.tif"
    iio.imwrite(image, np.zeros((16, 16), dtype=np.float32))
    iio.imwrite(labels, np.ones((16, 16), dtype=np.uint32))

    with Workflow(storage_path=str(tmp_path / "bif")) as wf:
        detected = DetectSpots()(
            input_image=image,
            method="local_maxima",
            threshold=1.0,
            name="detect",
        )
        assigned = AssignSpotsToLabels()(
            spot_id=detected["spot_id"],
            y=detected["y"],
            x=detected["x"],
            intensity=detected["intensity"],
            score=detected["score"],
            label_image=labels,
            name="assign",
        )
        result = wf.compute(assigned)

    assert result.empty
    assert list(result.columns) == [
        "spot_id",
        "y",
        "x",
        "intensity",
        "score",
        "label",
        "assigned_count",
    ]


def test_detect_spots_zero_rows_publish_blank_label_artifact(tmp_path: Path) -> None:
    image = tmp_path / "blank.tif"
    storage_path = tmp_path / "bif"
    iio.imwrite(image, np.zeros((16, 16), dtype=np.float32))

    with Workflow(storage_path=str(storage_path)) as wf:
        detected = DetectSpots()(
            input_image=image,
            method="local_maxima",
            threshold=1.0,
            name="detect",
        )
        result = wf.compute(detected)

    assert result.empty
    assert list(result.columns) == [
        "output_labels",
        "spot_id",
        "y",
        "x",
        "intensity",
        "score",
        "spot_count",
    ]
    [current_path] = list(storage_path.glob("cache/v1/results/**/current.json"))
    current = json.loads(current_path.read_text())
    record_dir = current_path.parent / "records" / current["record_id"]
    manifest = json.loads((record_dir / "manifest.json").read_text())
    assert manifest["outputs"] == [
        {
            "digest": manifest["outputs"][0]["digest"],
            "kind": "owned_asset",
            "output_column": "output_labels",
            "path": "assets/blank_spots.tif",
            "row_index": "0",
            "size": manifest["outputs"][0]["size"],
        }
    ]
    labels = iio.imread(record_dir / "assets" / "blank_spots.tif")
    assert labels.dtype == np.uint32
    assert labels.shape == (16, 16)
    assert int(labels.max()) == 0


def test_spot_colocalization_builds_workflow_graph(tmp_path: Path) -> None:
    image = _spot_image(tmp_path / "puncta.tif")

    with Workflow(storage_path=str(tmp_path / "bif")) as wf:
        detected = DetectSpots()(input_image=image, threshold=0.3, name="detect")
        matches = SpotColocalization()(
            detected,
            detected,
            max_distance=0.1,
            name="matches",
        )
        result = wf.compute(matches)

    assert len(result) == 3
    assert [str(group) for group in result["group"].unique()] == ["0"]
    assert set(result["reference_spot_id"]) == {1, 2, 3}
    assert set(result["query_spot_id"]) == {1, 2, 3}


def test_filter_spots_uses_numeric_thresholds_and_mask(tmp_path: Path) -> None:
    mask = np.zeros((16, 16), dtype=np.uint8)
    mask[0:10, 0:10] = 1
    mask_path = tmp_path / "mask.tif"
    iio.imwrite(mask_path, mask)

    rows = pd.DataFrame(
        {
            "spot_id": [1, 2, 3],
            "y": [5, 8, 12],
            "x": [5, 8, 12],
            "intensity": [4.0, 9.0, 11.0],
            "score": [0.4, 0.9, 0.7],
            "radius": [1.0, 2.0, 5.0],
            "source": ["a", "b", "c"],
        },
        index=_index(["image::0", "image::1", "image::2"]),
    )
    result = FilterSpots().transform(
        rows,
        Arguments(
            min_intensity=5.0,
            min_score=0.5,
            max_radius=3.0,
            mask_image=mask_path,
        ),
    )

    assert list(result["spot_id"]) == [2]
    assert list(result["source"]) == ["b"]
    assert list(result["spot_count"]) == [1]


def test_render_spots_and_spots_to_labels_create_label_images(tmp_path: Path) -> None:
    spots = [
        Arguments(spot_id=10, y=4, x=5, image_shape="16,16", radius=1, label_mode=True),
        Arguments(spot_id=11, y=9, x=12, image_shape="16,16", radius=1, label_mode=True),
    ]

    rendered = RenderSpots().process_batch(
        [Arguments(**vars(spot), output_image=tmp_path / "rendered.tif") for spot in spots]
    )[0][0]
    labels = SpotsToLabels().process_batch(
        [Arguments(**vars(spot), label_image=tmp_path / "labels.tif") for spot in spots]
    )[0][0]

    rendered_image = iio.imread(rendered.output_image)
    label_image = iio.imread(labels.label_image)
    assert rendered_image[4, 5] == 10
    assert label_image[9, 12] == 11
    assert rendered.spot_count == 2
    assert labels.label_count == 2


def test_render_spots_label_mode_false_writes_binary_uint8_mask(tmp_path: Path) -> None:
    result = RenderSpots().process_batch(
        [
            Arguments(
                spot_id=70000,
                y=4,
                x=5,
                image_shape="16,16",
                radius=1,
                label_mode=False,
                output_image=tmp_path / "mask.tif",
            )
        ]
    )[0][0]

    mask = iio.imread(result.output_image)
    assert mask.dtype == np.uint8
    assert set(np.unique(mask)) == {0, 1}
    assert result.spot_count == 1


def test_render_spots_zero_rows_preserves_artifact_and_count(tmp_path: Path) -> None:
    result = RenderSpots().process_batch(
        [
            Arguments(
                image_shape="16,16",
                radius=1,
                label_mode=True,
                output_image=tmp_path / "rendered.tif",
            )
        ]
    )[0][0]

    labels = iio.imread(result.output_image)
    assert labels.dtype == np.uint32
    assert int(labels.max()) == 0
    assert result.spot_count == 0


def test_render_spots_zero_rows_writes_one_artifact_per_anchor(tmp_path: Path) -> None:
    results = RenderSpots().process_batch(
        [
            Arguments(
                image_shape="16,16",
                radius=1,
                label_mode=True,
                output_image=tmp_path / f"rendered_{index}.tif",
            )
            for index in range(2)
        ]
    )

    assert len(results) == 2
    for row in results:
        result = row[0]
        labels = iio.imread(result.output_image)
        assert labels.dtype == np.uint32
        assert labels.shape == (16, 16)
        assert int(labels.max()) == 0
        assert result.spot_count == 0


def test_spots_to_labels_zero_rows_preserves_artifact_and_count(tmp_path: Path) -> None:
    result = SpotsToLabels().process_batch(
        [
            Arguments(
                image_shape="16,16",
                radius=1,
                label_image=tmp_path / "labels.tif",
            )
        ]
    )[0][0]

    labels = iio.imread(result.label_image)
    assert labels.dtype == np.uint32
    assert int(labels.max()) == 0
    assert result.label_count == 0


def test_spots_to_labels_mask_mode_writes_one_artifact_per_mask(tmp_path: Path) -> None:
    mask_paths = []
    for index in range(2):
        mask = np.zeros((16, 16), dtype=np.uint8)
        if index == 1:
            mask[4:6, 4:6] = 1
        mask_path = tmp_path / f"mask_{index}.tif"
        iio.imwrite(mask_path, mask)
        mask_paths.append(mask_path)

    results = SpotsToLabels().process_batch(
        [
            Arguments(
                mask_image=mask_path,
                label_image=tmp_path / f"labels_{index}.tif",
            )
            for index, mask_path in enumerate(mask_paths)
        ]
    )

    assert len(results) == 2
    assert [row[0].label_count for row in results] == [0, 1]
    assert int(iio.imread(results[0][0].label_image).max()) == 0
    assert int(iio.imread(results[1][0].label_image).max()) == 1


def test_spots_to_labels_zero_spot_workflow_writes_blank_artifact(
    tmp_path: Path,
) -> None:
    image = tmp_path / "blank.tif"
    iio.imwrite(image, np.zeros((16, 16), dtype=np.float32))

    with Workflow(storage_path=str(tmp_path / "bif")) as wf:
        detected = DetectSpots()(
            input_image=image,
            method="local_maxima",
            threshold=1.0,
            name="detect",
        )
        labels_node = SpotsToLabels()(
            spot_id=detected["spot_id"],
            y=detected["y"],
            x=detected["x"],
            image_shape="16,16",
            name="labels",
        )
        first = wf.compute(labels_node)
        second = wf.compute(labels_node)

    assert len(first) == 1
    assert len(second) == 1
    assert int(second.iloc[0]["label_count"]) == 0
    labels = iio.imread(second.iloc[0]["label_image"])
    assert labels.dtype == np.uint32
    assert labels.shape == (16, 16)
    assert int(labels.max()) == 0
    assert Path(second.iloc[0]["label_image"]).exists()


def test_spots_to_labels_empty_coordinates_do_not_treat_masks_as_fake_rows(
    tmp_path: Path,
) -> None:
    mask_dir = tmp_path / "masks"
    mask_dir.mkdir()
    for index in range(2):
        mask = np.zeros((16, 16), dtype=np.uint8)
        mask[4:6, 4:6] = 1
        iio.imwrite(mask_dir / f"mask_{index}.tif", mask)

    with Workflow(storage_path=str(tmp_path / "bif")) as wf:
        masks = Files()(path=mask_dir, pattern="*.tif", name="masks")
        detected = DetectSpots()(
            input_image=masks["path"],
            method="local_maxima",
            threshold=2.0,
            name="detect",
        )
        labels_node = SpotsToLabels()(
            spot_id=detected["spot_id"],
            y=detected["y"],
            x=detected["x"],
            mask_image=masks["path"],
            image_shape="16,16",
            name="labels",
        )
        result = wf.compute(labels_node)

    assert len(result) == 1
    assert int(result.iloc[0]["label_count"]) == 0
    labels = iio.imread(result.iloc[0]["label_image"])
    assert labels.shape == (16, 16)
    assert int(labels.max()) == 0


def test_spot_label_renderers_preserve_ids_above_uint16(tmp_path: Path) -> None:
    large_spot_id = int(np.iinfo(np.uint16).max) + 17
    spot = Arguments(
        spot_id=large_spot_id,
        y=4,
        x=5,
        image_shape="16,16",
        radius=0,
        label_mode=True,
    )

    rendered = RenderSpots().process_batch(
        [Arguments(**vars(spot), output_image=tmp_path / "rendered.tif")]
    )[0][0]
    labels = SpotsToLabels().process_batch(
        [Arguments(**vars(spot), label_image=tmp_path / "labels.tif")]
    )[0][0]

    rendered_image = iio.imread(rendered.output_image)
    label_image = iio.imread(labels.label_image)
    assert rendered_image.dtype == np.uint32
    assert label_image.dtype == np.uint32
    assert int(rendered_image[4, 5]) == large_spot_id
    assert int(label_image[4, 5]) == large_spot_id


def test_spot_label_renderers_accept_integer_like_label_ids(tmp_path: Path) -> None:
    rendered = RenderSpots().process_batch(
        [
            Arguments(
                spot_id="1.0",
                y=4,
                x=5,
                image_shape="16,16",
                radius=0,
                label_mode=True,
                output_image=tmp_path / "rendered.tif",
            )
        ]
    )[0][0]
    labels = SpotsToLabels().process_batch(
        [
            Arguments(
                spot_id=1.0,
                y=4,
                x=5,
                image_shape="16,16",
                radius=0,
                label_image=tmp_path / "labels.tif",
            )
        ]
    )[0][0]

    assert int(iio.imread(rendered.output_image)[4, 5]) == 1
    assert int(iio.imread(labels.label_image)[4, 5]) == 1


@pytest.mark.parametrize(
    "spot_id",
    [0, -1, 1.5, int(np.iinfo(np.uint32).max) + 1],
)
def test_spot_label_renderers_reject_invalid_label_ids(
    tmp_path: Path,
    spot_id: int | float,
) -> None:
    spot = Arguments(
        spot_id=spot_id,
        y=4,
        x=5,
        image_shape="16,16",
        radius=0,
        label_mode=True,
    )

    with pytest.raises(ValueError, match="positive integer|<="):
        RenderSpots().process_batch(
            [Arguments(**vars(spot), output_image=tmp_path / "rendered.tif")]
        )

    with pytest.raises(ValueError, match="positive integer|<="):
        SpotsToLabels().process_batch(
            [Arguments(**vars(spot), label_image=tmp_path / "labels.tif")]
        )


def test_spot_coordinate_tools_reject_missing_required_coordinates(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="required column 'x'"):
        RenderSpots().process_batch(
            [
                Arguments(
                    spot_id=1,
                    y=4,
                    image_shape="16,16",
                    radius=1,
                    label_mode=True,
                    output_image=tmp_path / "rendered.tif",
                )
            ]
        )

    with pytest.raises(ValueError, match="required column 'x'"):
        SpotsToLabels().process_batch(
            [
                Arguments(
                    spot_id=1,
                    y=4,
                    image_shape="16,16",
                    radius=1,
                    label_image=tmp_path / "labels.tif",
                )
            ]
        )


def test_spot_colocalization_matches_nearest_available_spots() -> None:
    reference = pd.DataFrame(
        {
            "spot_id": [1, 2, 3],
            "y": [5.0, 20.0, 10.0],
            "x": [5.0, 20.0, 10.0],
        },
        index=_index(["image_a::0", "image_a::1", "image_b::0"]),
    )
    query = pd.DataFrame(
        {
            "spot_id": [7, 8, 9],
            "y": [6.0, 28.0, 10.0],
            "x": [5.0, 20.0, 11.5],
        },
        index=_index(["image_a::0", "image_a::1", "image_b::0"]),
    )

    result = SpotColocalization().merge_dataframes(
        [reference, query],
        Arguments(max_distance=2.0),
    )

    assert list(result.index) == ["image_a::0", "image_b::0"]
    assert list(result["group"]) == ["image_a", "image_b"]
    assert list(zip(result["reference_spot_id"], result["query_spot_id"])) == [
        (1, 7),
        (3, 9),
    ]
    assert list(result["matched_count"]) == [1, 1]


def test_spot_colocalization_can_group_by_image_column() -> None:
    reference = pd.DataFrame(
        {
            "image_id": ["field_1", "field_2"],
            "spot_id": [1, 2],
            "y": [4.0, 20.0],
            "x": [4.0, 20.0],
        },
        index=_index(["ref_0", "ref_1"]),
    )
    query = pd.DataFrame(
        {
            "image_id": ["field_2", "field_1"],
            "spot_id": [10, 11],
            "y": [20.5, 4.5],
            "x": [20.0, 4.0],
        },
        index=_index(["query_0", "query_1"]),
    )

    result = SpotColocalization().merge_dataframes(
        [reference, query],
        Arguments(group_by="image_id", max_distance=1.0),
    )

    assert list(result["group"]) == ["field_1", "field_2"]
    assert list(zip(result["reference_spot_id"], result["query_spot_id"])) == [
        (1, 11),
        (2, 10),
    ]


def test_spot_colocalization_rejects_malformed_coordinates() -> None:
    with pytest.raises(ValueError, match="missing required column.*'x'"):
        SpotColocalization().merge_dataframes(
            [
                pd.DataFrame(
                    {"spot_id": [1], "y": [5.0]},
                    index=_index(["image::0"]),
                ),
                pd.DataFrame(
                    {"spot_id": [2], "y": [5.0], "x": [5.0]},
                    index=_index(["image::0"]),
                ),
            ],
            Arguments(max_distance=2.0),
        )


def test_spot_colocalization_rejects_unrelated_index_lineage() -> None:
    with pytest.raises(ValueError, match="no shared groups"):
        SpotColocalization().merge_dataframes(
            [
                pd.DataFrame(
                    {"spot_id": [1], "y": [5.0], "x": [5.0]},
                    index=_index(["reference_image::0"]),
                ),
                pd.DataFrame(
                    {"spot_id": [2], "y": [5.0], "x": [5.0]},
                    index=_index(["query_image::0"]),
                ),
            ],
            Arguments(max_distance=2.0),
        )


def test_spot_quality_metrics_reports_snr_and_nearest_neighbor(tmp_path: Path) -> None:
    image = np.ones((20, 20), dtype=np.float32)
    image[5, 5] = 11.0
    image[5, 9] = 7.0
    image_path = tmp_path / "image.tif"
    iio.imwrite(image_path, image)

    spots = pd.DataFrame(
        {
            "spot_id": [1, 2],
            "y": [5, 5],
            "x": [5, 9],
            "intensity": [11.0, 7.0],
            "channel": ["gfp", "gfp"],
        },
        index=_index(["image::0", "image::1"]),
    )
    result = SpotQualityMetrics().transform(
        spots,
        Arguments(image=str(image_path), radius=1),
    )

    assert list(result["nearest_neighbor_distance"]) == [4.0, 4.0]
    assert all(result["snr"] > 1.0)
    assert list(result["channel"]) == ["gfp", "gfp"]
    assert list(result["spot_count"]) == [2, 2]


def test_spot_quality_metrics_rejects_out_of_bounds_coordinates(tmp_path: Path) -> None:
    image = np.ones((10, 10), dtype=np.float32)
    image_path = tmp_path / "image.tif"
    iio.imwrite(image_path, image)

    with pytest.raises(ValueError, match="outside image bounds"):
        SpotQualityMetrics().transform(
            pd.DataFrame(
                {"spot_id": [1], "y": [15], "x": [5], "intensity": [2.0]},
                index=_index(["image::0"]),
            ),
            Arguments(image=str(image_path), radius=1),
        )
