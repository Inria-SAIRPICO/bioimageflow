from pathlib import Path

import imageio.v3 as iio
import numpy as np
import pandas as pd

from bioimageflow import Workflow
from bioimageflow_core import Arguments
from bioimageflow_spot_tools import AssignSpotsToLabels, DetectSpots, SpotSummary


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
