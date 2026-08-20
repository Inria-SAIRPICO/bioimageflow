"""Lightweight label and table measurement tools."""

from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any

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
    RowConsumption,
    Semantic,
)

if TYPE_CHECKING:
    from bioimageflow import DataFrameTool as DataFrameTool
else:
    try:
        from bioimageflow import DataFrameTool as DataFrameTool
    except ModuleNotFoundError:
        class DataFrameTool:  # type: ignore[no-redef]
            """Unavailable outside the orchestrator environment."""

            def __init__(self, *args: Any, **kwargs: Any) -> None:
                raise RuntimeError(
                    "DataFrameTool classes require the bioimageflow orchestrator package."
                )


class RegionProperties(ProcessingTool):
    """Compute geometric properties for each non-zero label."""

    row_consumption = RowConsumption.MAPPED
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


class ShapeProperties(ProcessingTool):
    """Compute extended deterministic shape features for each non-zero label."""

    row_consumption = RowConsumption.MAPPED
    display_name = "Shape Properties"
    documentation = (
        "Compute area, perimeter, bounding-box area, extent, aspect ratio, and "
        "equivalent diameter for each label."
    )
    category = Category.MEASUREMENT
    tags = ["measurement", "labels", "shape"]
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
        perimeter: Annotated[float, GUIMeta(display_name="Perimeter")]
        bbox_area: Annotated[int, GUIMeta(display_name="Bounding box area")]
        extent: Annotated[float, GUIMeta(display_name="Extent")]
        aspect_ratio: Annotated[float, GUIMeta(display_name="Aspect ratio")]
        equivalent_diameter: Annotated[
            float,
            GUIMeta(display_name="Equivalent diameter"),
        ]

    def process_row(self, arguments: Arguments, *, context: Any = None) -> Any:
        import math

        labels = _read_label_image(arguments.label_image)
        rows = []
        for region in _region_rows(labels):
            mask = labels == region["label"]
            bbox_height = int(region["bbox_max_y"] - region["bbox_min_y"] + 1)
            bbox_width = int(region["bbox_max_x"] - region["bbox_min_x"] + 1)
            bbox_area = bbox_height * bbox_width
            area = int(region["area"])
            rows.append(
                self.Outputs(
                    label=int(region["label"]),
                    area=area,
                    perimeter=float(_pixel_perimeter(mask)),
                    bbox_area=bbox_area,
                    extent=float(area / bbox_area) if bbox_area else 0.0,
                    aspect_ratio=float(bbox_width / bbox_height)
                    if bbox_height
                    else 0.0,
                    equivalent_diameter=float(math.sqrt(4.0 * area / math.pi)),
                )
            )
        return rows


class IntensityProperties(ProcessingTool):
    """Compute intensity statistics for each non-zero label."""

    row_consumption = RowConsumption.MAPPED
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

    row_consumption = RowConsumption.MAPPED
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

    row_consumption = RowConsumption.MAPPED
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


class DiceIoU(ProcessingTool):
    """Compute foreground Dice and IoU between predicted and reference labels."""

    row_consumption = RowConsumption.MAPPED
    display_name = "Dice IoU"
    documentation = "Compute binary foreground Dice and IoU metrics for two masks."
    category = Category.MEASUREMENT
    tags = ["measurement", "labels", "benchmark", "dice", "iou"]
    environment = GENERAL_ENV

    class Inputs(IOModel):
        predicted_label_image: Annotated[
            Path,
            ImageSpec(semantics={Semantic.LABEL, Semantic.BINARY}, layouts={Layout.PLANAR}),
            GUIMeta(
                display_name="Predicted label image",
                description="Predicted binary or label image.",
                connectable=Connectable.BY_DEFAULT,
            ),
        ]
        reference_label_image: Annotated[
            Path,
            ImageSpec(semantics={Semantic.LABEL, Semantic.BINARY}, layouts={Layout.PLANAR}),
            GUIMeta(
                display_name="Reference label image",
                description="Reference binary or label image.",
                connectable=Connectable.BY_DEFAULT,
            ),
        ]

    class Outputs(IOModel):
        true_positive_pixels: Annotated[int, GUIMeta(display_name="True positive pixels")]
        false_positive_pixels: Annotated[int, GUIMeta(display_name="False positive pixels")]
        false_negative_pixels: Annotated[int, GUIMeta(display_name="False negative pixels")]
        foreground_iou: Annotated[float, GUIMeta(display_name="Foreground IoU")]
        foreground_dice: Annotated[float, GUIMeta(display_name="Foreground Dice")]

    def process_row(self, arguments: Arguments, *, context: Any = None) -> Any:
        predicted = _read_label_image(arguments.predicted_label_image)
        reference = _read_label_image(arguments.reference_label_image)
        if predicted.shape != reference.shape:
            raise ValueError("predicted_label_image and reference_label_image must match.")
        metrics = _foreground_overlap(predicted > 0, reference > 0)
        return self.Outputs(**metrics)


class ObjectMatchingMetrics(ProcessingTool):
    """Greedily match predicted and reference objects by pairwise IoU."""

    row_consumption = RowConsumption.MAPPED
    display_name = "Object Matching Metrics"
    documentation = "Match predicted labels to reference labels and summarize IoU/Dice."
    category = Category.MEASUREMENT
    tags = ["measurement", "labels", "benchmark", "objects"]
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
        iou_threshold: Annotated[
            float,
            GUIMeta(
                display_name="IoU threshold",
                description="Minimum IoU required to count a predicted/reference match.",
                group="general",
            ),
        ] = 0.5

    class Outputs(IOModel):
        predicted_label_count: Annotated[int, GUIMeta(display_name="Predicted labels")]
        reference_label_count: Annotated[int, GUIMeta(display_name="Reference labels")]
        matched_count: Annotated[int, GUIMeta(display_name="Matched labels")]
        unmatched_predicted_count: Annotated[
            int,
            GUIMeta(display_name="Unmatched predicted labels"),
        ]
        unmatched_reference_count: Annotated[
            int,
            GUIMeta(display_name="Unmatched reference labels"),
        ]
        mean_matched_iou: Annotated[float, GUIMeta(display_name="Mean matched IoU")]
        mean_matched_dice: Annotated[float, GUIMeta(display_name="Mean matched Dice")]

    def process_row(self, arguments: Arguments, *, context: Any = None) -> Any:
        predicted = _read_label_image(arguments.predicted_label_image)
        reference = _read_label_image(arguments.reference_label_image)
        if predicted.shape != reference.shape:
            raise ValueError("predicted_label_image and reference_label_image must match.")

        predicted_labels = _nonzero_labels(predicted)
        reference_labels = _nonzero_labels(reference)
        matches = _greedy_label_matches(
            predicted,
            reference,
            iou_threshold=float(getattr(arguments, "iou_threshold", 0.5)),
        )
        ious = [match["iou"] for match in matches]
        dices = [match["dice"] for match in matches]
        return self.Outputs(
            predicted_label_count=len(predicted_labels),
            reference_label_count=len(reference_labels),
            matched_count=len(matches),
            unmatched_predicted_count=len(predicted_labels) - len(matches),
            unmatched_reference_count=len(reference_labels) - len(matches),
            mean_matched_iou=float(sum(ious) / len(ious)) if ious else 0.0,
            mean_matched_dice=float(sum(dices) / len(dices)) if dices else 0.0,
        )


class AggregatePerImage(DataFrameTool):
    """Aggregate object-level rows into per-image summaries."""

    display_name = "Aggregate Per Image"
    documentation = "Aggregate numeric object-level feature columns by image/sample."
    category = Category.MEASUREMENT
    tags = ["measurement", "table", "aggregate"]

    class Inputs(IOModel):
        group_by: Annotated[str, GUIMeta(
            display_name="Group by",
            description="Image/sample identifier column.",
            connectable=Connectable.NEVER,
        )] = "image"
        columns: Annotated[str | None, GUIMeta(
            display_name="Columns",
            description="Comma-separated numeric columns. Empty means all numeric columns.",
            connectable=Connectable.NEVER,
        )] = None
        stats: Annotated[str, GUIMeta(
            display_name="Statistics",
            description="Comma-separated pandas aggregations.",
            connectable=Connectable.NEVER,
        )] = "count,mean,min,max,sum"

    class Outputs(IOModel):
        pass

    def transform(self, df: Any, arguments: Arguments) -> Any:
        import pandas as pd

        table = pd.DataFrame(df)
        group_by = getattr(arguments, "group_by", "image")
        if group_by not in table.columns:
            raise ValueError(f"Unknown group_by column: {group_by}")
        columns = _requested_columns(table.drop(columns=[group_by]), arguments.columns)
        stats = _requested_stats(getattr(arguments, "stats", "count,mean,min,max,sum"))
        result = table.groupby(group_by, dropna=False)[columns].agg(stats)
        result.columns = [f"{column}_{stat}" for column, stat in result.columns]
        result.insert(
            0,
            "object_count",
            table.groupby(group_by, dropna=False).size().to_numpy(),
        )
        return result.reset_index()


class NormalizeFeatures(DataFrameTool):
    """Normalize numeric feature columns in a table."""

    display_name = "Normalize Features"
    documentation = "Append normalized feature columns using z-score, robust, or min-max."
    category = Category.MEASUREMENT
    tags = ["measurement", "table", "normalize"]

    class Inputs(IOModel):
        columns: Annotated[str | None, GUIMeta(
            display_name="Columns",
            description="Comma-separated numeric columns. Empty means all numeric columns.",
            connectable=Connectable.NEVER,
        )] = None
        method: Annotated[str, GUIMeta(
            display_name="Method",
            description="Normalization method: zscore, robust, or minmax.",
            connectable=Connectable.NEVER,
        )] = "zscore"
        suffix: Annotated[str, GUIMeta(
            display_name="Suffix",
            description="Suffix appended to normalized columns.",
            connectable=Connectable.NEVER,
        )] = "_normalized"

    class Outputs(IOModel):
        pass

    def transform(self, df: Any, arguments: Arguments) -> Any:
        import pandas as pd

        table = pd.DataFrame(df).copy()
        columns = _requested_columns(table, arguments.columns)
        method = str(getattr(arguments, "method", "zscore")).lower()
        if method not in {"zscore", "robust", "minmax"}:
            raise ValueError("method must be one of: zscore, robust, minmax")

        for column in columns:
            values = table[column].astype(float)
            if method == "zscore":
                center = values.mean()
                scale = values.std(ddof=0)
            elif method == "robust":
                center = values.median()
                scale = values.quantile(0.75) - values.quantile(0.25)
            else:
                center = values.min()
                scale = values.max() - values.min()
            output_column = f"{column}{getattr(arguments, 'suffix', '_normalized')}"
            table[output_column] = 0.0 if scale == 0 else (values - center) / scale
        return table


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


def _pixel_perimeter(mask: Any) -> int:
    import numpy as np

    foreground = np.asarray(mask, dtype=bool)
    perimeter = 0
    for axis in range(foreground.ndim):
        perimeter += int(np.take(foreground, 0, axis=axis).sum())
        perimeter += int(np.take(foreground, -1, axis=axis).sum())
        perimeter += int(np.diff(foreground.astype(np.int8), axis=axis).astype(bool).sum())
    return perimeter


def _foreground_overlap(predicted: Any, reference: Any) -> dict[str, int | float]:
    tp = int((predicted & reference).sum())
    fp = int((predicted & ~reference).sum())
    fn = int((~predicted & reference).sum())
    union = tp + fp + fn
    dice_denominator = (2 * tp) + fp + fn
    return {
        "true_positive_pixels": tp,
        "false_positive_pixels": fp,
        "false_negative_pixels": fn,
        "foreground_iou": float(tp / union) if union else 1.0,
        "foreground_dice": float((2 * tp) / dice_denominator)
        if dice_denominator
        else 1.0,
    }


def _greedy_label_matches(
    predicted: Any,
    reference: Any,
    *,
    iou_threshold: float,
) -> list[dict[str, float | int]]:
    candidates = []
    for predicted_label in _nonzero_labels(predicted):
        predicted_mask = predicted == predicted_label
        predicted_area = int(predicted_mask.sum())
        for reference_label in _nonzero_labels(reference):
            reference_mask = reference == reference_label
            intersection = int((predicted_mask & reference_mask).sum())
            if intersection == 0:
                continue
            reference_area = int(reference_mask.sum())
            union = predicted_area + reference_area - intersection
            iou = float(intersection / union)
            if iou < iou_threshold:
                continue
            dice = float((2 * intersection) / (predicted_area + reference_area))
            candidates.append(
                {
                    "predicted_label": predicted_label,
                    "reference_label": reference_label,
                    "iou": iou,
                    "dice": dice,
                }
            )

    matches = []
    used_predicted: set[int] = set()
    used_reference: set[int] = set()
    for candidate in sorted(
        candidates,
        key=lambda item: (
            -float(item["iou"]),
            int(item["predicted_label"]),
            int(item["reference_label"]),
        ),
    ):
        predicted_label = int(candidate["predicted_label"])
        reference_label = int(candidate["reference_label"])
        if predicted_label in used_predicted or reference_label in used_reference:
            continue
        used_predicted.add(predicted_label)
        used_reference.add(reference_label)
        matches.append(candidate)
    return matches


def _requested_columns(table: Any, columns: str | None) -> list[str]:
    if columns:
        requested = [column.strip() for column in columns.split(",") if column.strip()]
    else:
        requested = list(table.select_dtypes(include="number").columns)
    missing = [column for column in requested if column not in table.columns]
    if missing:
        raise ValueError(f"Unknown summary columns: {missing}")
    return requested


def _requested_stats(stats: str) -> list[str]:
    requested = [stat.strip() for stat in stats.split(",") if stat.strip()]
    allowed = {"count", "mean", "median", "min", "max", "sum", "std"}
    invalid = [stat for stat in requested if stat not in allowed]
    if invalid:
        raise ValueError(f"Unsupported aggregation stats: {invalid}")
    return requested
