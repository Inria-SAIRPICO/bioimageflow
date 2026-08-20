"""Shared label-image validation and measurement helpers."""

from pathlib import Path
from typing import Any


def read_label_image(path: Path) -> Any:
    """Read a planar label raster and return a validated integer array."""
    import imageio.v3 as iio
    import numpy as np

    labels = np.asarray(iio.imread(path))
    if labels.ndim != 2:
        raise ValueError("label image must be 2D.")
    is_bool = np.issubdtype(labels.dtype, np.bool_)
    is_integer = np.issubdtype(labels.dtype, np.integer)
    is_float = np.issubdtype(labels.dtype, np.floating)
    if not (is_bool or is_integer or is_float):
        raise ValueError("label image must contain real numeric values.")
    if not np.isfinite(labels).all():
        raise ValueError("label image must contain only finite values.")
    if is_float and not np.equal(labels, np.floor(labels)).all():
        raise ValueError("label image values must be integers.")
    if (labels < 0).any():
        raise ValueError("label image values must be non-negative.")

    maximum = int(labels.max(initial=0))
    if maximum > int(np.iinfo(np.int64).max):
        raise ValueError("label image values exceed the supported integer range.")
    return labels.astype(np.int64, copy=False)


def dense_labels(labels: Any) -> tuple[Any, Any]:
    """Densify positive IDs for regionprops without allocating up to max(label)."""
    import numpy as np

    original_ids = np.unique(labels)
    original_ids = original_ids[original_ids > 0]
    if not original_ids.size:
        return np.zeros(labels.shape, dtype=np.int32), original_ids
    dense = np.searchsorted(original_ids, labels).astype(np.int64, copy=False) + 1
    dense[labels == 0] = 0
    return dense, original_ids


def nonzero_labels(labels: Any) -> list[int]:
    """Return sorted, distinct positive label IDs."""
    import numpy as np

    return [int(value) for value in np.unique(labels) if value > 0]


def foreground_overlap(predicted: Any, reference: Any) -> dict[str, int | float]:
    """Compute binary foreground confusion counts, IoU, and Dice once."""
    predicted_fg = predicted > 0
    reference_fg = reference > 0
    true_positive = int((predicted_fg & reference_fg).sum())
    false_positive = int((predicted_fg & ~reference_fg).sum())
    false_negative = int((~predicted_fg & reference_fg).sum())
    union = true_positive + false_positive + false_negative
    dice_denominator = (2 * true_positive) + false_positive + false_negative
    return {
        "true_positive_pixels": true_positive,
        "false_positive_pixels": false_positive,
        "false_negative_pixels": false_negative,
        "foreground_iou": float(true_positive / union) if union else 1.0,
        "foreground_dice": (
            float((2 * true_positive) / dice_denominator)
            if dice_denominator
            else 1.0
        ),
    }


def greedy_label_matches(
    predicted: Any,
    reference: Any,
    *,
    iou_threshold: float,
) -> list[dict[str, float | int]]:
    """Build one overlap contingency and greedily match pairs by descending IoU."""
    import numpy as np

    predicted_ids, predicted_inverse, predicted_areas = np.unique(
        predicted, return_inverse=True, return_counts=True
    )
    reference_ids, reference_inverse, reference_areas = np.unique(
        reference, return_inverse=True, return_counts=True
    )
    reference_count = len(reference_ids)
    pair_indices = predicted_inverse * reference_count + reference_inverse
    pair_indices, intersections = np.unique(pair_indices, return_counts=True)

    candidates: list[dict[str, float | int]] = []
    for pair_index, intersection in zip(pair_indices, intersections, strict=True):
        predicted_index, reference_index = divmod(int(pair_index), reference_count)
        predicted_label = int(predicted_ids[predicted_index])
        reference_label = int(reference_ids[reference_index])
        if predicted_label == 0 or reference_label == 0:
            continue
        predicted_area = int(predicted_areas[predicted_index])
        reference_area = int(reference_areas[reference_index])
        intersection_count = int(intersection)
        union = predicted_area + reference_area - intersection_count
        iou = float(intersection_count / union)
        if iou < iou_threshold:
            continue
        candidates.append(
            {
                "predicted_label": predicted_label,
                "reference_label": reference_label,
                "iou": iou,
                "dice": float(
                    (2 * intersection_count) / (predicted_area + reference_area)
                ),
            }
        )

    matches: list[dict[str, float | int]] = []
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
