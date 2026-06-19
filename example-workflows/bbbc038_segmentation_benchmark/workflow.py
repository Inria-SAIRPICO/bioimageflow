"""Tiny BBBC038-style nuclei segmentation benchmark workflow.

The default builder writes a synthetic image/reference pair so tests can run
without downloading the public BBBC038 data. To evaluate real BBBC038 images,
replace the generated input paths with downloaded images and masks from the
Broad Bioimage Benchmark Collection.
"""

from pathlib import Path
from typing import Annotated, Any

import imageio.v3 as iio
import numpy as np

from bioimageflow import Workflow
from bioimageflow.node import Node
from bioimageflow_core import Arguments, Category, GENERAL_ENV, GUIMeta, IOModel, ProcessingTool


def _write_synthetic_bbbc038_fixture(data_dir: Path) -> tuple[Path, Path]:
    data_dir.mkdir(parents=True, exist_ok=True)
    yy, xx = np.mgrid[0:64, 0:64]
    image = np.zeros((64, 64), dtype=np.float32)
    reference = np.zeros((64, 64), dtype=np.uint16)
    objects = [(20, 22, 8, 1), (42, 40, 10, 2)]
    for cy, cx, radius, label in objects:
        mask = (yy - cy) ** 2 + (xx - cx) ** 2 <= radius**2
        image[mask] = 1.0
        reference[mask] = label
    image += np.linspace(0.0, 0.1, image.shape[1], dtype=np.float32)

    image_path = data_dir / "synthetic_bbbc038_image.tif"
    reference_path = data_dir / "synthetic_bbbc038_reference.tif"
    iio.imwrite(image_path, image)
    iio.imwrite(reference_path, reference)
    return image_path, reference_path


class SyntheticBBBC038Benchmark(ProcessingTool):
    """Write deterministic method masks and benchmark them against a reference."""

    display_name = "Synthetic BBBC038 Benchmark"
    category = Category.MEASUREMENT
    environment = GENERAL_ENV

    class Inputs(IOModel):
        input_image: Annotated[Path, GUIMeta(display_name="Input image")]
        reference_label_image: Annotated[Path, GUIMeta(display_name="Reference labels")]
        output_dir: Annotated[Path, GUIMeta(display_name="Output directory")]

    class Outputs(IOModel):
        method: Annotated[str, GUIMeta(display_name="Method")]
        input_image: Annotated[str, GUIMeta(display_name="Input image")]
        predicted_label_image: Annotated[str, GUIMeta(display_name="Predicted labels")]
        reference_label_image: Annotated[str, GUIMeta(display_name="Reference labels")]
        predicted_label_count: Annotated[int, GUIMeta(display_name="Predicted labels")]
        reference_label_count: Annotated[int, GUIMeta(display_name="Reference labels")]
        true_positive_pixels: Annotated[int, GUIMeta(display_name="True positive pixels")]
        false_positive_pixels: Annotated[int, GUIMeta(display_name="False positive pixels")]
        false_negative_pixels: Annotated[int, GUIMeta(display_name="False negative pixels")]
        foreground_iou: Annotated[float, GUIMeta(display_name="Foreground IoU")]
        foreground_dice: Annotated[float, GUIMeta(display_name="Foreground Dice")]

    def process_row(self, arguments: Arguments, *, context: Any = None) -> Any:
        image = np.asarray(iio.imread(arguments.input_image), dtype=np.float32)
        reference = np.asarray(iio.imread(arguments.reference_label_image), dtype=np.uint16)
        output_dir = Path(arguments.output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        classical = _label_binary(image > 0.5)
        masks = {
            "cellpose3": classical.copy(),
            "cellpose_sam": _shift_label(classical, dy=0, dx=1),
            "stardist": _shift_label(classical, dy=1, dx=0),
            "classical_threshold": classical.copy(),
        }
        rows = []
        for method, mask in masks.items():
            mask_path = output_dir / f"{method}_mask.tif"
            iio.imwrite(mask_path, mask.astype(np.uint16))
            metrics = _foreground_metrics(mask, reference)
            rows.append(
                self.Outputs(
                    method=method,
                    input_image=str(arguments.input_image),
                    predicted_label_image=str(mask_path),
                    reference_label_image=str(arguments.reference_label_image),
                    predicted_label_count=_label_count(mask),
                    reference_label_count=_label_count(reference),
                    **metrics,
                )
            )
        return rows


def _shift_label(labels: np.ndarray, *, dy: int, dx: int) -> np.ndarray:
    shifted = np.zeros_like(labels)
    src_y = slice(max(0, -dy), labels.shape[0] - max(0, dy))
    src_x = slice(max(0, -dx), labels.shape[1] - max(0, dx))
    dst_y = slice(max(0, dy), labels.shape[0] - max(0, -dy))
    dst_x = slice(max(0, dx), labels.shape[1] - max(0, -dx))
    shifted[dst_y, dst_x] = labels[src_y, src_x]
    return shifted


def _label_binary(mask: np.ndarray) -> np.ndarray:
    try:
        from scipy import ndimage as ndi

        labels, _ = ndi.label(mask)
        return labels.astype(np.uint16)
    except ImportError:
        labels = np.zeros(mask.shape, dtype=np.uint16)
        current = 0
        for y, x in zip(*np.nonzero(mask)):
            if labels[y, x] != 0:
                continue
            current += 1
            stack = [(int(y), int(x))]
            labels[y, x] = current
            while stack:
                cy, cx = stack.pop()
                for ny, nx in ((cy - 1, cx), (cy + 1, cx), (cy, cx - 1), (cy, cx + 1)):
                    if (
                        0 <= ny < mask.shape[0]
                        and 0 <= nx < mask.shape[1]
                        and mask[ny, nx]
                        and labels[ny, nx] == 0
                    ):
                        labels[ny, nx] = current
                        stack.append((ny, nx))
        return labels


def _label_count(labels: np.ndarray) -> int:
    return int(len([value for value in np.unique(labels) if value > 0]))


def _foreground_metrics(predicted: np.ndarray, reference: np.ndarray) -> dict[str, float | int]:
    predicted_fg = predicted > 0
    reference_fg = reference > 0
    tp = int((predicted_fg & reference_fg).sum())
    fp = int((predicted_fg & ~reference_fg).sum())
    fn = int((~predicted_fg & reference_fg).sum())
    union = tp + fp + fn
    dice_denominator = (2 * tp) + fp + fn
    return {
        "true_positive_pixels": tp,
        "false_positive_pixels": fp,
        "false_negative_pixels": fn,
        "foreground_iou": float(tp / union) if union else 1.0,
        "foreground_dice": float((2 * tp) / dice_denominator) if dice_denominator else 1.0,
    }


def build_workflow(
    storage_path: str = "./bbbc038_segmentation_results",
    engine: str = "wetlands",
    wetlands_config: dict | None = None,
) -> tuple[Workflow, Node]:
    """Build the synthetic BBBC038-style benchmark workflow."""
    storage = Path(storage_path)
    image_path, reference_path = _write_synthetic_bbbc038_fixture(storage / "data")

    wf = Workflow(
        storage_path=str(storage / "bif"),
        engine=engine,
        wetlands_config=wetlands_config,
    )
    with wf:
        benchmark = SyntheticBBBC038Benchmark()(
            input_image=image_path,
            reference_label_image=reference_path,
            output_dir=storage / "masks",
            name="benchmark_segmentation_methods",
        )
    return wf, benchmark


if __name__ == "__main__":
    workflow, terminal = build_workflow()
    print(workflow.compute(terminal).to_string(index=False))
