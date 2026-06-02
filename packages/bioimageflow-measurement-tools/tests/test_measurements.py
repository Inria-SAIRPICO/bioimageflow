from __future__ import annotations

from pathlib import Path

import imageio.v3 as iio
import numpy as np
import pandas as pd
import pytest

from bioimageflow.validation import serialize_input_schema, serialize_output_schema
from bioimageflow_core import Arguments

pytestmark = pytest.mark.package_tools


def test_measurement_tools_schema_and_synthetic_execution(tmp_path: Path) -> None:
    from bioimageflow_measurement_tools import (
        AggregatePerImage,
        CountLabels,
        DiceIoU,
        IntensityProperties,
        LabelBenchmark,
        NormalizeFeatures,
        ObjectMatchingMetrics,
        RegionProperties,
        ShapeProperties,
        SummarizeTable,
    )

    labels = np.zeros((5, 6), dtype=np.uint16)
    labels[1:3, 1:4] = 1
    labels[3:5, 2:5] = 2
    intensity = np.arange(labels.size, dtype=np.float32).reshape(labels.shape)

    labels_path = tmp_path / "labels.tif"
    intensity_path = tmp_path / "intensity.tif"
    iio.imwrite(labels_path, labels)
    iio.imwrite(intensity_path, intensity)

    for tool in [
        RegionProperties,
        ShapeProperties,
        IntensityProperties,
        CountLabels,
        SummarizeTable,
        LabelBenchmark,
        ObjectMatchingMetrics,
        DiceIoU,
        AggregatePerImage,
        NormalizeFeatures,
    ]:
        assert serialize_input_schema(tool)
        assert serialize_output_schema(tool) is not None

    assert "area" in serialize_output_schema(RegionProperties)

    regions = RegionProperties().process_row(Arguments(label_image=labels_path))
    assert [(row.label, row.area) for row in regions] == [(1, 6), (2, 6)]
    assert regions[0].bbox_min_y == 1
    assert regions[0].bbox_max_x == 3

    shapes = ShapeProperties().process_row(Arguments(label_image=labels_path))
    assert [(row.label, row.area, row.perimeter) for row in shapes] == [
        (1, 6, 10.0),
        (2, 6, 10.0),
    ]
    assert shapes[0].bbox_area == 6
    assert shapes[0].extent == 1.0
    assert shapes[0].aspect_ratio == 1.5

    measurements = IntensityProperties().process_row(
        Arguments(label_image=labels_path, intensity_image=intensity_path)
    )
    assert [row.label for row in measurements] == [1, 2]
    assert measurements[0].mean_intensity == float(intensity[labels == 1].mean())

    counts = CountLabels().process_row(Arguments(label_image=labels_path))
    assert counts.label_count == 2
    assert counts.object_pixel_count == 12

    table = pd.DataFrame(
        {
            "sample": ["a", "a", "b"],
            "area": [1.0, 3.0, 5.0],
            "score": [2.0, 4.0, 6.0],
        }
    )
    summary = SummarizeTable().transform(
        table,
        Arguments(group_by="sample", columns="area,score"),
    )
    assert list(summary.columns) == [
        "sample",
        "area_count",
        "area_mean",
        "area_min",
        "area_max",
        "area_sum",
        "score_count",
        "score_mean",
        "score_min",
        "score_max",
        "score_sum",
    ]
    assert summary.loc[summary["sample"] == "a", "area_mean"].item() == 2.0

    predicted = tmp_path / "predicted.tif"
    np_predicted = labels.copy()
    np_predicted[0, 0] = 3
    iio.imwrite(predicted, np_predicted)
    benchmark = LabelBenchmark().process_row(
        Arguments(predicted_label_image=predicted, reference_label_image=labels_path)
    )
    assert benchmark.predicted_label_count == 3
    assert benchmark.reference_label_count == 2
    assert benchmark.true_positive_pixels == 12

    matching = ObjectMatchingMetrics().process_row(
        Arguments(
            predicted_label_image=predicted,
            reference_label_image=labels_path,
            iou_threshold=0.5,
        )
    )
    assert matching.matched_count == 2
    assert matching.unmatched_predicted_count == 1
    assert matching.unmatched_reference_count == 0
    assert matching.mean_matched_iou == pytest.approx(1.0)
    assert matching.mean_matched_dice == pytest.approx(1.0)

    shifted = labels.copy()
    shifted[3:5, 4] = 0
    shifted[0, 0] = 3
    shifted_path = tmp_path / "shifted.tif"
    iio.imwrite(shifted_path, shifted)
    dice_iou = DiceIoU().process_row(
        Arguments(predicted_label_image=shifted_path, reference_label_image=labels_path)
    )
    assert dice_iou.true_positive_pixels == 10
    assert dice_iou.false_positive_pixels == 1
    assert dice_iou.false_negative_pixels == 2
    assert dice_iou.foreground_iou == pytest.approx(10 / 13)
    assert dice_iou.foreground_dice == pytest.approx(20 / 23)

    per_image = AggregatePerImage().transform(
        table,
        Arguments(group_by="sample", columns="area,score", stats="count,mean,sum"),
    )
    assert list(per_image.columns) == [
        "sample",
        "object_count",
        "area_count",
        "area_mean",
        "area_sum",
        "score_count",
        "score_mean",
        "score_sum",
    ]
    assert per_image.loc[per_image["sample"] == "a", "object_count"].item() == 2
    assert per_image.loc[per_image["sample"] == "b", "score_sum"].item() == 6.0

    normalized = NormalizeFeatures().transform(
        table,
        Arguments(columns="area,score", method="minmax"),
    )
    assert list(normalized["area_normalized"]) == [0.0, 0.5, 1.0]
    assert list(normalized["score_normalized"]) == [0.0, 0.5, 1.0]
