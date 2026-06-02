from pathlib import Path

import imageio.v3 as iio
import numpy as np
import pandas as pd
import pytest

from bioimageflow import Workflow
from bioimageflow_core import Arguments
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
            spots_csv=str(tmp_path / "spots.csv"),
        )
    )

    assert result.spot_count == 3
    labels = iio.imread(result.output_labels)
    assert labels.max() == 3
    spots = pd.read_csv(result.spots_csv)
    assert set(spots.columns) == {"spot_id", "y", "x", "intensity", "score"}
    assert len(spots) == 3


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
            spots_csv=str(tmp_path / "log_spots.csv"),
        )
    )

    assert result.spot_count == 3
    spots = pd.read_csv(result.spots_csv)
    assert len(spots) == 3
    assert (spots["score"] > 0).all()


def test_assign_spots_to_labels_and_summarize(tmp_path: Path) -> None:
    spots_csv = tmp_path / "spots.csv"
    pd.DataFrame(
        [
            {"spot_id": 1, "y": 6, "x": 6, "intensity": 10.0, "score": 4.0},
            {"spot_id": 2, "y": 10, "x": 10, "intensity": 12.0, "score": 5.0},
            {"spot_id": 3, "y": 30, "x": 30, "intensity": 8.0, "score": 3.0},
        ]
    ).to_csv(spots_csv, index=False)
    labels = np.zeros((40, 40), dtype=np.uint16)
    labels[0:20, 0:20] = 1
    labels[25:35, 25:35] = 2
    label_path = tmp_path / "labels.tif"
    iio.imwrite(label_path, labels)

    assigned = AssignSpotsToLabels().process_row(
        Arguments(
            spots_csv=str(spots_csv),
            label_image=str(label_path),
            output_csv=str(tmp_path / "assigned.csv"),
        )
    )
    summary = SpotSummary().process_row(
        Arguments(
            assigned_spots_csv=str(assigned.assigned_spots_csv),
            output_csv=str(tmp_path / "summary.csv"),
        )
    )

    table = pd.read_csv(assigned.assigned_spots_csv)
    assert table["label"].tolist() == [1, 1, 2]
    summary_table = pd.read_csv(summary.summary_csv).sort_values("label")
    assert summary_table["spot_count"].tolist() == [2, 1]
    assert summary_table["total_intensity"].tolist() == [22.0, 8.0]


def test_spot_tools_build_workflow_graph(tmp_path: Path) -> None:
    image = _spot_image(tmp_path / "puncta.tif")
    labels = np.ones((48, 48), dtype=np.uint16)
    labels_path = tmp_path / "labels.tif"
    iio.imwrite(labels_path, labels)

    with Workflow(storage_path=str(tmp_path / "bif")) as wf:
        detected = DetectSpots()(input_image=image, threshold=0.3, name="detect")
        assigned = AssignSpotsToLabels()(
            spots_csv=detected["spots_csv"],
            label_image=labels_path,
            name="assign",
        )
        summary = SpotSummary()(
            assigned_spots_csv=assigned["assigned_spots_csv"],
            name="summary",
        )
        result = wf.compute(summary)

    assert len(result) == 1
    assert Path(result.iloc[0]["summary_csv"]).exists()


def test_filter_spots_uses_numeric_thresholds_and_mask(tmp_path: Path) -> None:
    spots_csv = tmp_path / "spots.csv"
    pd.DataFrame(
        [
            {"spot_id": 1, "y": 5, "x": 5, "intensity": 4.0, "score": 0.4, "radius": 1.0},
            {"spot_id": 2, "y": 8, "x": 8, "intensity": 9.0, "score": 0.9, "radius": 2.0},
            {"spot_id": 3, "y": 12, "x": 12, "intensity": 11.0, "score": 0.7, "radius": 5.0},
        ]
    ).to_csv(spots_csv, index=False)
    mask = np.zeros((16, 16), dtype=np.uint8)
    mask[0:10, 0:10] = 1
    mask_path = tmp_path / "mask.tif"
    iio.imwrite(mask_path, mask)

    result = FilterSpots().process_row(
        Arguments(
            spots_csv=spots_csv,
            min_intensity=5.0,
            min_score=0.5,
            max_radius=3.0,
            mask_image=mask_path,
            filtered_spots_csv=tmp_path / "filtered.csv",
        )
    )

    table = pd.read_csv(result.filtered_spots_csv)
    assert table["spot_id"].tolist() == [2]
    assert result.spot_count == 1


def test_render_spots_and_spots_to_labels_create_label_images(tmp_path: Path) -> None:
    spots_csv = tmp_path / "spots.csv"
    pd.DataFrame(
        [
            {"spot_id": 10, "y": 4, "x": 5, "intensity": 8.0},
            {"spot_id": 11, "y": 9, "x": 12, "intensity": 7.0},
        ]
    ).to_csv(spots_csv, index=False)

    rendered = RenderSpots().process_row(
        Arguments(
            spots_csv=spots_csv,
            image_shape="16,16",
            radius=1,
            label_mode=True,
            output_image=tmp_path / "rendered.tif",
        )
    )
    labels = SpotsToLabels().process_row(
        Arguments(
            spots_csv=spots_csv,
            image_shape="16,16",
            radius=1,
            label_image=tmp_path / "labels.tif",
        )
    )

    rendered_image = iio.imread(rendered.output_image)
    label_image = iio.imread(labels.label_image)
    assert rendered_image[4, 5] == 10
    assert label_image[9, 12] == 11
    assert rendered.spot_count == 2
    assert labels.label_count == 2


def test_spot_coordinate_tools_reject_missing_required_coordinates(
    tmp_path: Path,
) -> None:
    bad_spots_csv = tmp_path / "bad_spots.csv"
    pd.DataFrame([{"spot_id": 1, "y": 4}]).to_csv(bad_spots_csv, index=False)

    with pytest.raises(ValueError, match="required column 'x'"):
        RenderSpots().process_row(
            Arguments(
                spots_csv=bad_spots_csv,
                image_shape="16,16",
                radius=1,
                label_mode=True,
                output_image=tmp_path / "rendered.tif",
            )
        )

    with pytest.raises(ValueError, match="required column 'x'"):
        SpotsToLabels().process_row(
            Arguments(
                spots_csv=bad_spots_csv,
                image_shape="16,16",
                radius=1,
                label_image=tmp_path / "labels.tif",
            )
        )


def test_spot_colocalization_matches_nearest_available_spots(tmp_path: Path) -> None:
    reference_csv = tmp_path / "reference.csv"
    query_csv = tmp_path / "query.csv"
    pd.DataFrame(
        [
            {"spot_id": 1, "y": 5.0, "x": 5.0},
            {"spot_id": 2, "y": 20.0, "x": 20.0},
        ]
    ).to_csv(reference_csv, index=False)
    pd.DataFrame(
        [
            {"spot_id": 7, "y": 6.0, "x": 5.0},
            {"spot_id": 8, "y": 28.0, "x": 20.0},
        ]
    ).to_csv(query_csv, index=False)

    result = SpotColocalization().process_row(
        Arguments(
            reference_spots_csv=reference_csv,
            query_spots_csv=query_csv,
            max_distance=2.0,
            matches_csv=tmp_path / "matches.csv",
        )
    )

    matches = pd.read_csv(result.matches_csv)
    assert matches[["reference_spot_id", "query_spot_id"]].values.tolist() == [[1, 7]]
    assert result.matched_count == 1


def test_spot_colocalization_rejects_malformed_coordinates(tmp_path: Path) -> None:
    reference_csv = tmp_path / "reference.csv"
    query_csv = tmp_path / "query.csv"
    pd.DataFrame([{"spot_id": 1, "y": 5.0}]).to_csv(reference_csv, index=False)
    pd.DataFrame([{"spot_id": 2, "y": 5.0, "x": 5.0}]).to_csv(query_csv, index=False)

    with pytest.raises(ValueError, match="required column 'x'"):
        SpotColocalization().process_row(
            Arguments(
                reference_spots_csv=reference_csv,
                query_spots_csv=query_csv,
                max_distance=2.0,
                matches_csv=tmp_path / "matches.csv",
            )
        )


def test_spot_quality_metrics_writes_snr_and_nearest_neighbor(tmp_path: Path) -> None:
    image = np.ones((20, 20), dtype=np.float32)
    image[5, 5] = 11.0
    image[5, 9] = 7.0
    image_path = tmp_path / "image.tif"
    iio.imwrite(image_path, image)
    spots_csv = tmp_path / "spots.csv"
    pd.DataFrame(
        [
            {"spot_id": 1, "y": 5, "x": 5, "intensity": 11.0},
            {"spot_id": 2, "y": 5, "x": 9, "intensity": 7.0},
        ]
    ).to_csv(spots_csv, index=False)

    result = SpotQualityMetrics().process_row(
        Arguments(
            spots_csv=spots_csv,
            image=str(image_path),
            radius=1,
            metrics_csv=tmp_path / "quality.csv",
        )
    )

    metrics = pd.read_csv(result.metrics_csv).sort_values("spot_id")
    assert metrics["nearest_neighbor_distance"].tolist() == [4.0, 4.0]
    assert (metrics["snr"] > 1.0).all()
    assert result.spot_count == 2


def test_spot_quality_metrics_rejects_out_of_bounds_coordinates(tmp_path: Path) -> None:
    image = np.ones((10, 10), dtype=np.float32)
    image_path = tmp_path / "image.tif"
    iio.imwrite(image_path, image)
    spots_csv = tmp_path / "spots.csv"
    pd.DataFrame([{"spot_id": 1, "y": 15, "x": 5, "intensity": 2.0}]).to_csv(
        spots_csv,
        index=False,
    )

    with pytest.raises(ValueError, match="outside image bounds"):
        SpotQualityMetrics().process_row(
            Arguments(
                spots_csv=spots_csv,
                image=str(image_path),
                radius=1,
                metrics_csv=tmp_path / "quality.csv",
            )
        )
