"""Worker-safe label and image measurement tools."""

from pathlib import Path
from typing import Annotated, Any

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

from ._labels import (
    dense_labels,
    foreground_overlap,
    greedy_label_matches,
    nonzero_labels,
    read_label_image,
)


class RegionProperties(ProcessingTool):
    """Compute geometric properties for each positive label."""

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
                description="2D non-negative integer label image to measure.",
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
        from skimage.measure import regionprops_table

        labels = read_label_image(arguments.label_image)
        dense, original_ids = dense_labels(labels)
        properties = regionprops_table(
            dense,
            properties=("label", "area", "centroid", "bbox"),
        )
        return [
            self.Outputs(
                label=int(original_ids[int(dense_label) - 1]),
                area=int(area),
                centroid_y=float(centroid_y),
                centroid_x=float(centroid_x),
                bbox_min_y=int(min_y),
                bbox_min_x=int(min_x),
                bbox_max_y=int(max_y) - 1,
                bbox_max_x=int(max_x) - 1,
            )
            for dense_label, area, centroid_y, centroid_x, min_y, min_x, max_y, max_x in zip(
                properties["label"],
                properties["area"],
                properties["centroid-0"],
                properties["centroid-1"],
                properties["bbox-0"],
                properties["bbox-1"],
                properties["bbox-2"],
                properties["bbox-3"],
                strict=True,
            )
        ]


class ShapeProperties(ProcessingTool):
    """Compute standard shape features for each positive label."""

    row_consumption = RowConsumption.MAPPED
    display_name = "Shape Properties"
    documentation = (
        "Compute area, contour perimeter, bounding-box area, extent, aspect ratio, "
        "and equivalent diameter for each label."
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
                description="2D non-negative integer label image to measure.",
                connectable=Connectable.BY_DEFAULT,
            ),
        ]

    class Outputs(IOModel):
        label: Annotated[int, GUIMeta(display_name="Label")]
        area: Annotated[int, GUIMeta(display_name="Area")]
        perimeter: Annotated[float, GUIMeta(display_name="Contour perimeter")]
        bbox_area: Annotated[int, GUIMeta(display_name="Bounding box area")]
        extent: Annotated[float, GUIMeta(display_name="Extent")]
        aspect_ratio: Annotated[float, GUIMeta(display_name="Aspect ratio")]
        equivalent_diameter: Annotated[
            float,
            GUIMeta(display_name="Equivalent diameter"),
        ]

    def process_row(self, arguments: Arguments, *, context: Any = None) -> Any:
        from skimage.measure import regionprops

        labels = read_label_image(arguments.label_image)
        dense, original_ids = dense_labels(labels)
        rows = []
        for region in regionprops(dense):
            min_y, min_x, max_y, max_x = region.bbox
            bbox_height = max_y - min_y
            bbox_width = max_x - min_x
            rows.append(
                self.Outputs(
                    label=int(original_ids[region.label - 1]),
                    area=int(region.area),
                    perimeter=float(region.perimeter),
                    bbox_area=int(region.area_bbox),
                    extent=float(region.extent),
                    aspect_ratio=float(bbox_width / bbox_height),
                    equivalent_diameter=float(region.equivalent_diameter_area),
                )
            )
        return rows


class IntensityProperties(ProcessingTool):
    """Compute intensity statistics for each positive label."""

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
                description="2D non-negative integer label image defining regions.",
                connectable=Connectable.BY_DEFAULT,
            ),
        ]
        intensity_image: Annotated[
            Path,
            ImageSpec(semantics={Semantic.INTENSITY}, layouts={Layout.PLANAR}),
            GUIMeta(
                display_name="Intensity image",
                description="Finite 2D intensity image to measure within labels.",
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
        from skimage.measure import regionprops

        labels = read_label_image(arguments.label_image)
        intensities = np.asarray(iio.imread(arguments.intensity_image), dtype=float)
        if labels.shape != intensities.shape:
            raise ValueError("label_image and intensity_image must have the same shape.")
        if not np.isfinite(intensities).all():
            raise ValueError("intensity_image must contain only finite values.")
        dense, original_ids = dense_labels(labels)
        return [
            self.Outputs(
                label=int(original_ids[region.label - 1]),
                mean_intensity=float(region.intensity_mean),
                min_intensity=float(region.intensity_min),
                max_intensity=float(region.intensity_max),
                sum_intensity=float(region.intensity_mean * region.area),
            )
            for region in regionprops(dense, intensity_image=intensities)
        ]


class CountLabels(ProcessingTool):
    """Count positive labels and labeled pixels."""

    row_consumption = RowConsumption.MAPPED
    display_name = "Count Labels"
    documentation = "Count positive labels and object pixels in a label image."
    category = Category.MEASUREMENT
    tags = ["measurement", "labels", "count"]
    environment = GENERAL_ENV

    class Inputs(IOModel):
        label_image: Annotated[
            Path,
            ImageSpec(semantics={Semantic.LABEL}, layouts={Layout.PLANAR}),
            GUIMeta(
                display_name="Label image",
                description="2D non-negative integer label image to count.",
                connectable=Connectable.BY_DEFAULT,
            ),
        ]

    class Outputs(IOModel):
        label_count: Annotated[int, GUIMeta(display_name="Label count")]
        object_pixel_count: Annotated[int, GUIMeta(display_name="Object pixel count")]

    def process_row(self, arguments: Arguments, *, context: Any = None) -> Any:
        labels = read_label_image(arguments.label_image)
        return self.Outputs(
            label_count=len(nonzero_labels(labels)),
            object_pixel_count=int((labels > 0).sum()),
        )


class LabelBenchmark(ProcessingTool):
    """Compute pixel-level foreground agreement between two label images."""

    row_consumption = RowConsumption.MAPPED
    display_name = "Label Benchmark"
    documentation = "Compute foreground pixel agreement for two label images."
    category = Category.MEASUREMENT
    tags = ["measurement", "labels", "benchmark"]
    environment = GENERAL_ENV

    class Inputs(IOModel):
        predicted_label_image: Annotated[
            Path,
            ImageSpec(semantics={Semantic.LABEL}, layouts={Layout.PLANAR}),
            GUIMeta(display_name="Predicted label image", connectable=Connectable.BY_DEFAULT),
        ]
        reference_label_image: Annotated[
            Path,
            ImageSpec(semantics={Semantic.LABEL}, layouts={Layout.PLANAR}),
            GUIMeta(display_name="Reference label image", connectable=Connectable.BY_DEFAULT),
        ]

    class Outputs(IOModel):
        predicted_label_count: Annotated[int, GUIMeta(display_name="Predicted labels")]
        reference_label_count: Annotated[int, GUIMeta(display_name="Reference labels")]
        true_positive_pixels: Annotated[int, GUIMeta(display_name="True positive pixels")]
        false_positive_pixels: Annotated[int, GUIMeta(display_name="False positive pixels")]
        false_negative_pixels: Annotated[int, GUIMeta(display_name="False negative pixels")]
        foreground_iou: Annotated[float, GUIMeta(display_name="Foreground IoU")]

    def process_row(self, arguments: Arguments, *, context: Any = None) -> Any:
        predicted = read_label_image(arguments.predicted_label_image)
        reference = read_label_image(arguments.reference_label_image)
        _require_matching_shapes(predicted, reference)
        metrics = foreground_overlap(predicted, reference)
        return self.Outputs(
            predicted_label_count=len(nonzero_labels(predicted)),
            reference_label_count=len(nonzero_labels(reference)),
            true_positive_pixels=metrics["true_positive_pixels"],
            false_positive_pixels=metrics["false_positive_pixels"],
            false_negative_pixels=metrics["false_negative_pixels"],
            foreground_iou=metrics["foreground_iou"],
        )


class DiceIoU(ProcessingTool):
    """Compute foreground Dice and IoU between two binary or label images."""

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
            GUIMeta(display_name="Predicted label image", connectable=Connectable.BY_DEFAULT),
        ]
        reference_label_image: Annotated[
            Path,
            ImageSpec(semantics={Semantic.LABEL, Semantic.BINARY}, layouts={Layout.PLANAR}),
            GUIMeta(display_name="Reference label image", connectable=Connectable.BY_DEFAULT),
        ]

    class Outputs(IOModel):
        true_positive_pixels: Annotated[int, GUIMeta(display_name="True positive pixels")]
        false_positive_pixels: Annotated[int, GUIMeta(display_name="False positive pixels")]
        false_negative_pixels: Annotated[int, GUIMeta(display_name="False negative pixels")]
        foreground_iou: Annotated[float, GUIMeta(display_name="Foreground IoU")]
        foreground_dice: Annotated[float, GUIMeta(display_name="Foreground Dice")]

    def process_row(self, arguments: Arguments, *, context: Any = None) -> Any:
        predicted = read_label_image(arguments.predicted_label_image)
        reference = read_label_image(arguments.reference_label_image)
        _require_matching_shapes(predicted, reference)
        return self.Outputs(**foreground_overlap(predicted, reference))


class ObjectMatchingMetrics(ProcessingTool):
    """Greedily match predicted and reference objects by pairwise IoU."""

    row_consumption = RowConsumption.MAPPED
    display_name = "Object Matching Metrics"
    documentation = "Greedily match labels by descending IoU and summarize IoU/Dice."
    category = Category.MEASUREMENT
    tags = ["measurement", "labels", "benchmark", "objects"]
    environment = GENERAL_ENV

    class Inputs(IOModel):
        predicted_label_image: Annotated[
            Path,
            ImageSpec(semantics={Semantic.LABEL}, layouts={Layout.PLANAR}),
            GUIMeta(display_name="Predicted label image", connectable=Connectable.BY_DEFAULT),
        ]
        reference_label_image: Annotated[
            Path,
            ImageSpec(semantics={Semantic.LABEL}, layouts={Layout.PLANAR}),
            GUIMeta(display_name="Reference label image", connectable=Connectable.BY_DEFAULT),
        ]
        iou_threshold: Annotated[
            float,
            GUIMeta(
                display_name="IoU threshold",
                description="Minimum IoU required to count a match (0 to 1).",
                group="general",
                min=0.0,
                max=1.0,
            ),
        ] = 0.5

    class Outputs(IOModel):
        predicted_label_count: Annotated[int, GUIMeta(display_name="Predicted labels")]
        reference_label_count: Annotated[int, GUIMeta(display_name="Reference labels")]
        matched_count: Annotated[int, GUIMeta(display_name="Matched labels")]
        unmatched_predicted_count: Annotated[int, GUIMeta(display_name="Unmatched predicted labels")]
        unmatched_reference_count: Annotated[int, GUIMeta(display_name="Unmatched reference labels")]
        mean_matched_iou: Annotated[float, GUIMeta(display_name="Mean matched IoU")]
        mean_matched_dice: Annotated[float, GUIMeta(display_name="Mean matched Dice")]

    def process_row(self, arguments: Arguments, *, context: Any = None) -> Any:
        import math

        predicted = read_label_image(arguments.predicted_label_image)
        reference = read_label_image(arguments.reference_label_image)
        _require_matching_shapes(predicted, reference)
        threshold = float(getattr(arguments, "iou_threshold", 0.5))
        if not math.isfinite(threshold) or not 0.0 <= threshold <= 1.0:
            raise ValueError("iou_threshold must be finite and between 0 and 1.")
        predicted_labels = nonzero_labels(predicted)
        reference_labels = nonzero_labels(reference)
        matches = greedy_label_matches(predicted, reference, iou_threshold=threshold)
        ious = [float(match["iou"]) for match in matches]
        dices = [float(match["dice"]) for match in matches]
        return self.Outputs(
            predicted_label_count=len(predicted_labels),
            reference_label_count=len(reference_labels),
            matched_count=len(matches),
            unmatched_predicted_count=len(predicted_labels) - len(matches),
            unmatched_reference_count=len(reference_labels) - len(matches),
            mean_matched_iou=sum(ious) / len(ious) if ious else 0.0,
            mean_matched_dice=sum(dices) / len(dices) if dices else 0.0,
        )


def _require_matching_shapes(predicted: Any, reference: Any) -> None:
    if predicted.shape != reference.shape:
        raise ValueError("predicted_label_image and reference_label_image must match.")
