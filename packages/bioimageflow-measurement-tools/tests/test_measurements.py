from __future__ import annotations

from pathlib import Path

import imageio.v3 as iio
import numpy as np
import pandas as pd
import pytest

from bioimageflow.validation import serialize_input_schema, serialize_output_schema
from bioimageflow_core import Arguments
from bioimageflow_core import BaseTool

pytestmark = pytest.mark.package_tools


def test_measurement_package_all_exports_only_public_tools() -> None:
    import bioimageflow_measurement_tools as measurement

    assert sorted(measurement.__all__) == [
        "AggregatePerImage",
        "CountLabels",
        "DiceIoU",
        "IntensityProperties",
        "LabelBenchmark",
        "NormalizeFeatures",
        "ObjectMatchingMetrics",
        "RegionProperties",
        "ShapeProperties",
        "SummarizeTable",
    ]
    assert "measurements" not in measurement.__all__
    assert all(issubclass(getattr(measurement, name), BaseTool) for name in measurement.__all__)


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
        (1, 6, 6.0),
        (2, 6, 6.0),
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


@pytest.mark.parametrize(
    ("values", "message"),
    [
        (np.array([[0.0, 1.5]], dtype=np.float32), "integers"),
        (np.array([[0.0, -1.0]], dtype=np.float32), "non-negative"),
        (np.array([[0.0, np.nan]], dtype=np.float32), "finite"),
    ],
)
def test_label_tools_reject_invalid_label_rasters(
    tmp_path: Path,
    values: np.ndarray,
    message: str,
) -> None:
    from bioimageflow_measurement_tools import CountLabels

    path = tmp_path / "invalid.tif"
    iio.imwrite(path, values)
    with pytest.raises(ValueError, match=message):
        CountLabels().process_row(Arguments(label_image=path))


def test_region_properties_supports_sparse_label_ids(tmp_path: Path) -> None:
    from bioimageflow_measurement_tools import RegionProperties

    labels = np.array([[0, 1_000_000_000], [7, 7]], dtype=np.uint32)
    path = tmp_path / "sparse.tif"
    iio.imwrite(path, labels)
    rows = RegionProperties().process_row(Arguments(label_image=path))
    assert [(row.label, row.area) for row in rows] == [(7, 2), (1_000_000_000, 1)]


def test_count_labels_accepts_boolean_masks(tmp_path: Path) -> None:
    from bioimageflow_measurement_tools import CountLabels

    path = tmp_path / "mask.tif"
    iio.imwrite(path, np.array([[False, True], [True, False]]))
    result = CountLabels().process_row(Arguments(label_image=path))
    assert (result.label_count, result.object_pixel_count) == (1, 2)


@pytest.mark.parametrize("threshold", [-0.1, 1.1, np.nan, np.inf])
def test_object_matching_rejects_invalid_iou_thresholds(
    tmp_path: Path,
    threshold: float,
) -> None:
    from bioimageflow_measurement_tools import ObjectMatchingMetrics

    path = tmp_path / "labels.tif"
    iio.imwrite(path, np.array([[0, 1]], dtype=np.uint8))
    with pytest.raises(ValueError, match="between 0 and 1"):
        ObjectMatchingMetrics().process_row(
            Arguments(
                predicted_label_image=path,
                reference_label_image=path,
                iou_threshold=threshold,
            )
        )


def test_table_tools_validate_selection_and_output_collisions() -> None:
    from bioimageflow_measurement_tools import (
        AggregatePerImage,
        NormalizeFeatures,
        SummarizeTable,
    )

    table = pd.DataFrame({"image": ["a", "a"], "area": [1, 2], "bad": ["x", "y"]})
    with pytest.raises(ValueError, match="at least one column"):
        SummarizeTable().transform(table, Arguments(columns=""))
    with pytest.raises(ValueError, match="numeric"):
        SummarizeTable().transform(table, Arguments(columns="bad"))
    with pytest.raises(ValueError, match="at least one aggregation"):
        AggregatePerImage().transform(
            table, Arguments(group_by="image", columns="area", stats="")
        )
    with pytest.raises(ValueError, match="Duplicate aggregation"):
        AggregatePerImage().transform(
            table, Arguments(group_by="image", columns="area", stats="mean,mean")
        )

    colliding = table.assign(area_normalized=3.0)
    with pytest.raises(ValueError, match="collisions"):
        NormalizeFeatures().transform(colliding, Arguments(columns="area"))


def test_normalization_preserves_missing_values_and_zeroes_constants() -> None:
    from bioimageflow_measurement_tools import NormalizeFeatures

    table = pd.DataFrame({"constant": [2.0, np.nan, 2.0], "missing": [np.nan] * 3})
    result = NormalizeFeatures().transform(table, Arguments(columns="constant,missing"))
    assert result["constant_normalized"].tolist()[::2] == [0.0, 0.0]
    assert pd.isna(result.loc[1, "constant_normalized"])
    assert result["missing_normalized"].isna().all()


def test_table_tools_resolve_configured_output_schemas() -> None:
    from bioimageflow_measurement_tools import (
        AggregatePerImage,
        NormalizeFeatures,
        SummarizeTable,
    )

    upstream = {
        "sample": {"type": "str", "default": None, "image_spec": None},
        "area": {"type": "float", "default": None, "image_spec": None},
    }
    summary = SummarizeTable.resolve_merge_schema(
        [upstream], {"group_by": "sample", "columns": "area"}
    )
    assert summary is not None
    assert list(summary) == [
        "sample",
        "area_count",
        "area_mean",
        "area_min",
        "area_max",
        "area_sum",
    ]
    aggregate = AggregatePerImage.resolve_merge_schema(
        [upstream], {"group_by": "sample", "columns": "area", "stats": "mean,sum"}
    )
    assert aggregate is not None
    assert list(aggregate) == ["sample", "object_count", "area_mean", "area_sum"]
    normalized = NormalizeFeatures.resolve_merge_schema(
        [upstream], {"columns": "area", "suffix": "_z"}
    )
    assert normalized is not None
    assert list(normalized) == ["sample", "area", "area_z"]
