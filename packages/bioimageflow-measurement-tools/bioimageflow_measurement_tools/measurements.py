"""Lightweight label and table measurement tools."""

from pathlib import Path
from typing import Annotated, Any

from bioimageflow import DataFrameTool
from bioimageflow_core import (
    Arguments,
    Category,
    Connectable,
    GENERAL_ENV,
    GUIMeta,
    ImageSpec,
    IOModel,
    Layout,
    ProcessingTool,
    Semantic,
)


class RegionProperties(ProcessingTool):
    """Compute geometric properties for each non-zero label."""

    display_name = "Region Properties"
    documentation = "Compute area, centroid, and bounding box for each label."
    category = Category.MEASUREMENT
    tags = ["measurement", "labels", "regions"]
    environment = GENERAL_ENV

    class Inputs(IOModel):
        label_image: Annotated[
            Path,
            ImageSpec(semantics={Semantic.LABEL}, layouts={Layout.PLANAR}),
            GUIMeta(
                display_name="Label image",
                description="2D label image to measure.",
                connectable=Connectable.BY_DEFAULT,
            ),
        ]

    class Outputs(IOModel):
        label: Annotated[int, GUIMeta(display_name="Label")]
        area: Annotated[int, GUIMeta(display_name="Area")]
        centroid_y: Annotated[float, GUIMeta(display_name="Centroid Y")]
        centroid_x: Annotated[float, GUIMeta(display_name="Centroid X")]
        bbox_min_y: Annotated[int, GUIMeta(display_name="Bounding box min Y")]
        bbox_min_x: Annotated[int, GUIMeta(display_name="Bounding box min X")]
        bbox_max_y: Annotated[int, GUIMeta(display_name="Bounding box max Y")]
        bbox_max_x: Annotated[int, GUIMeta(display_name="Bounding box max X")]

    def process_row(self, arguments: Arguments, *, context: Any = None) -> Any:
        labels = _read_label_image(arguments.label_image)
        return [
            self.Outputs(
                label=region["label"],
                area=region["area"],
                centroid_y=region["centroid_y"],
                centroid_x=region["centroid_x"],
                bbox_min_y=region["bbox_min_y"],
                bbox_min_x=region["bbox_min_x"],
                bbox_max_y=region["bbox_max_y"],
                bbox_max_x=region["bbox_max_x"],
            )
            for region in _region_rows(labels)
        ]


class IntensityProperties(ProcessingTool):
    """Compute intensity statistics for each non-zero label."""

    display_name = "Intensity Properties"
    documentation = "Compute mean, min, max, and sum intensity for each label."
    category = Category.MEASUREMENT
    tags = ["measurement", "labels", "intensity"]
    environment = GENERAL_ENV

    class Inputs(IOModel):
        label_image: Annotated[
            Path,
            ImageSpec(semantics={Semantic.LABEL}, layouts={Layout.PLANAR}),
            GUIMeta(
                display_name="Label image",
                description="2D label image defining regions.",
                connectable=Connectable.BY_DEFAULT,
            ),
        ]
        intensity_image: Annotated[
            Path,
            ImageSpec(semantics={Semantic.INTENSITY}, layouts={Layout.PLANAR}),
            GUIMeta(
                display_name="Intensity image",
                description="2D intensity image to measure within labels.",
                connectable=Connectable.BY_DEFAULT,
            ),
        ]

    class Outputs(IOModel):
        label: Annotated[int, GUIMeta(display_name="Label")]
        mean_intensity: Annotated[float, GUIMeta(display_name="Mean intensity")]
        min_intensity: Annotated[float, GUIMeta(display_name="Min intensity")]
        max_intensity: Annotated[float, GUIMeta(display_name="Max intensity")]
        sum_intensity: Annotated[float, GUIMeta(display_name="Sum intensity")]

    def process_row(self, arguments: Arguments, *, context: Any = None) -> Any:
        import imageio.v3 as iio
        import numpy as np

        labels = _read_label_image(arguments.label_image)
        intensities = np.asarray(iio.imread(arguments.intensity_image), dtype=float)
        if labels.shape != intensities.shape:
            raise ValueError("label_image and intensity_image must have the same shape.")
        rows = []
        for label in _nonzero_labels(labels):
            values = intensities[labels == label]
            rows.append(
                self.Outputs(
                    label=int(label),
                    mean_intensity=float(values.mean()),
                    min_intensity=float(values.min()),
                    max_intensity=float(values.max()),
                    sum_intensity=float(values.sum()),
                )
            )
        return rows


class CountLabels(ProcessingTool):
    """Count non-zero labels and labeled pixels."""

    display_name = "Count Labels"
    documentation = "Count non-zero labels and object pixels in a label image."
    category = Category.MEASUREMENT
    tags = ["measurement", "labels", "count"]
    environment = GENERAL_ENV

    class Inputs(IOModel):
        label_image: Annotated[
            Path,
            ImageSpec(semantics={Semantic.LABEL}, layouts={Layout.PLANAR}),
            GUIMeta(
                display_name="Label image",
                description="2D label image to count.",
                connectable=Connectable.BY_DEFAULT,
            ),
        ]

    class Outputs(IOModel):
        label_count: Annotated[int, GUIMeta(display_name="Label count")]
        object_pixel_count: Annotated[int, GUIMeta(display_name="Object pixel count")]

    def process_row(self, arguments: Arguments, *, context: Any = None) -> Any:
        labels = _read_label_image(arguments.label_image)
        return self.Outputs(
            label_count=len(_nonzero_labels(labels)),
            object_pixel_count=int((labels > 0).sum()),
        )


class SummarizeTable(DataFrameTool):
    """Summarize numeric columns in an upstream table."""

    display_name = "Summarize Table"
    documentation = "Summarize numeric columns with count, mean, min, max, and sum."
    category = Category.MEASUREMENT
    tags = ["measurement", "table", "summary"]

    class Inputs(IOModel):
        group_by: Annotated[str | None, GUIMeta(
            display_name="Group by",
            description="Optional grouping column.",
            connectable=Connectable.NEVER,
        )] = None
        columns: Annotated[str | None, GUIMeta(
            display_name="Columns",
            description="Comma-separated numeric columns. Empty means all numeric columns.",
            connectable=Connectable.NEVER,
        )] = None

    class Outputs(IOModel):
        pass

    def transform(self, df: Any, arguments: Arguments) -> Any:
        import pandas as pd

        table = pd.DataFrame(df)
        columns = _requested_columns(table, arguments.columns)
        grouped_by = arguments.group_by
        if grouped_by:
            result = table.groupby(grouped_by, dropna=False)[columns].agg(
                ["count", "mean", "min", "max", "sum"]
            )
            result.columns = [f"{column}_{stat}" for column, stat in result.columns]
            return result.reset_index()

        summary = table[columns].agg(["count", "mean", "min", "max", "sum"]).T
        summary.insert(0, "column", summary.index)
        summary = summary.reset_index(drop=True)
        return summary.rename(
            columns={
                stat: f"value_{stat}" for stat in summary.columns if stat != "column"
            }
        )


class LabelBenchmark(ProcessingTool):
    """Compute simple pixel-level agreement between predicted and reference labels."""

    display_name = "Label Benchmark"
    documentation = "Compute simple foreground pixel agreement for two label images."
    category = Category.MEASUREMENT
    tags = ["measurement", "labels", "benchmark"]
    environment = GENERAL_ENV

    class Inputs(IOModel):
        predicted_label_image: Annotated[
            Path,
            ImageSpec(semantics={Semantic.LABEL}, layouts={Layout.PLANAR}),
            GUIMeta(
                display_name="Predicted label image",
                description="Predicted label image.",
                connectable=Connectable.BY_DEFAULT,
            ),
        ]
        reference_label_image: Annotated[
            Path,
            ImageSpec(semantics={Semantic.LABEL}, layouts={Layout.PLANAR}),
            GUIMeta(
                display_name="Reference label image",
                description="Reference label image.",
                connectable=Connectable.BY_DEFAULT,
            ),
        ]

    class Outputs(IOModel):
        predicted_label_count: Annotated[int, GUIMeta(display_name="Predicted labels")]
        reference_label_count: Annotated[int, GUIMeta(display_name="Reference labels")]
        true_positive_pixels: Annotated[int, GUIMeta(display_name="True positive pixels")]
        false_positive_pixels: Annotated[int, GUIMeta(display_name="False positive pixels")]
        false_negative_pixels: Annotated[int, GUIMeta(display_name="False negative pixels")]
        foreground_iou: Annotated[float, GUIMeta(display_name="Foreground IoU")]

    def process_row(self, arguments: Arguments, *, context: Any = None) -> Any:
        predicted = _read_label_image(arguments.predicted_label_image)
        reference = _read_label_image(arguments.reference_label_image)
        if predicted.shape != reference.shape:
            raise ValueError("predicted_label_image and reference_label_image must match.")

        predicted_fg = predicted > 0
        reference_fg = reference > 0
        tp = int((predicted_fg & reference_fg).sum())
        fp = int((predicted_fg & ~reference_fg).sum())
        fn = int((~predicted_fg & reference_fg).sum())
        union = tp + fp + fn
        return self.Outputs(
            predicted_label_count=len(_nonzero_labels(predicted)),
            reference_label_count=len(_nonzero_labels(reference)),
            true_positive_pixels=tp,
            false_positive_pixels=fp,
            false_negative_pixels=fn,
            foreground_iou=float(tp / union) if union else 1.0,
        )


def _read_label_image(path: Path) -> Any:
    import imageio.v3 as iio
    import numpy as np

    labels = np.asarray(iio.imread(path))
    if labels.ndim != 2:
        raise ValueError("label image must be 2D.")
    return labels


def _nonzero_labels(labels: Any) -> list[int]:
    import numpy as np

    return [int(label) for label in np.unique(labels) if label != 0]


def _region_rows(labels: Any) -> list[dict[str, int | float]]:
    import numpy as np

    rows: list[dict[str, int | float]] = []
    for label in _nonzero_labels(labels):
        ys, xs = np.nonzero(labels == label)
        rows.append(
            {
                "label": int(label),
                "area": int(ys.size),
                "centroid_y": float(ys.mean()),
                "centroid_x": float(xs.mean()),
                "bbox_min_y": int(ys.min()),
                "bbox_min_x": int(xs.min()),
                "bbox_max_y": int(ys.max()),
                "bbox_max_x": int(xs.max()),
            }
        )
    return rows


def _requested_columns(table: Any, columns: str | None) -> list[str]:
    if columns:
        requested = [column.strip() for column in columns.split(",") if column.strip()]
    else:
        requested = list(table.select_dtypes(include="number").columns)
    missing = [column for column in requested if column not in table.columns]
    if missing:
        raise ValueError(f"Unknown summary columns: {missing}")
    return requested
