"""BBBC038 nuclei segmentation benchmark workflow.

The workflow consumes a BBBC038 ``stage1_train``-style subset where each sample
has one raw image under ``images/`` and one or more instance masks under
``masks/``.
"""

import argparse
from pathlib import Path
from typing import Annotated, Any

import imageio.v3 as iio
import numpy as np
import pandas as pd

from bioimageflow import DataFrameTool, Workflow
from bioimageflow_core import (
    Arguments,
    Category,
    Connectable,
    GENERAL_ENV,
    GUIMeta,
    IOModel,
    ProcessingTool,
    RowConsumption,
)
from bioimageflow_common_tools import Concat
from bioimageflow_segmentation_tools import (
    Cellpose3,
    CellposeSAM,
    StarDistSegmenter,
    ThresholdSegment,
)

EXAMPLE_WORKFLOWS_DIR = Path(__file__).resolve().parents[1]
DEFAULT_DATA_DIR = EXAMPLE_WORKFLOWS_DIR / "bbbc038_segmentation_benchmark" / "data"
DEFAULT_STORAGE_PATH = Path(__file__).resolve().parent / "results"


class BBBC038Samples(DataFrameTool):
    """List BBBC038 stage1_train samples from a local subset."""

    display_name = "BBBC038 Samples"
    category = Category.UTILITIES
    accepts_upstream = False

    class Inputs(IOModel):
        data_dir: Annotated[
            Path,
            GUIMeta(
                display_name="Data directory",
                description="Directory containing stage1_train/<sample>/images and masks folders.",
                connectable=Connectable.NEVER,
            ),
        ]
        sample_glob: Annotated[
            str,
            GUIMeta(display_name="Sample glob", connectable=Connectable.NEVER),
        ] = "*"

    class Outputs(IOModel):
        sample_id: Annotated[str, GUIMeta(display_name="Sample ID")]
        input_image: Annotated[str, GUIMeta(display_name="Input image")]
        mask_dir: Annotated[str, GUIMeta(display_name="Mask directory")]

    def transform(self, df: Any, arguments: Any) -> pd.DataFrame:
        root = Path(arguments.data_dir)
        stage_dir = root / "stage1_train"
        if not stage_dir.exists():
            raise FileNotFoundError(f"BBBC038 stage1_train directory not found: {stage_dir}")

        rows: list[dict[str, str]] = []
        for sample_dir in sorted(path for path in stage_dir.glob(arguments.sample_glob) if path.is_dir()):
            images = sorted((sample_dir / "images").glob("*"))
            mask_dir = sample_dir / "masks"
            if not images or not mask_dir.is_dir():
                continue
            rows.append(
                {
                    "sample_id": sample_dir.name,
                    "input_image": str(images[0]),
                    "mask_dir": str(mask_dir),
                }
            )
        if not rows:
            raise ValueError(f"No BBBC038 samples found in {stage_dir}.")
        return pd.DataFrame(rows)


class BuildBBBC038ReferenceLabels(ProcessingTool):
    """Combine BBBC038 per-object mask files into one instance label image."""

    row_consumption = RowConsumption.MAPPED
    display_name = "Build BBBC038 Reference Labels"
    category = Category.CONVERSION
    environment = GENERAL_ENV

    class Inputs(IOModel):
        mask_dir: Annotated[Path, GUIMeta(display_name="Mask directory")]
        sample_id: Annotated[str, GUIMeta(display_name="Sample ID")]

    class Outputs(IOModel):
        reference_label_image: Annotated[str, GUIMeta(display_name="Reference labels")]

    def process_row(self, arguments: Arguments, *, context: Any = None) -> Any:
        mask_paths = sorted(Path(arguments.mask_dir).glob("*"))
        if not mask_paths:
            raise ValueError(f"No BBBC038 mask files found in {arguments.mask_dir}.")

        labels: np.ndarray | None = None
        for label_id, mask_path in enumerate(mask_paths, start=1):
            mask = np.asarray(iio.imread(mask_path)) > 0
            if labels is None:
                labels = np.zeros(mask.shape, dtype=np.uint16)
            if labels.shape != mask.shape:
                raise ValueError(
                    f"Mask shape mismatch in {mask_path}: {mask.shape} != {labels.shape}."
                )
            labels[(labels == 0) & mask] = label_id

        if labels is None:
            raise ValueError(f"No usable masks found in {arguments.mask_dir}.")
        output = Path(f"{arguments.sample_id}_reference_labels.tif")
        if context is not None and getattr(context, "assets_dir", None):
            output = Path(context.assets_dir) / output
        output.parent.mkdir(parents=True, exist_ok=True)
        iio.imwrite(output, labels)
        return self.Outputs(reference_label_image=str(output))


class PrepareBBBC038SegmentationImage(ProcessingTool):
    """Convert BBBC038 image files to a 2D intensity image for segmentation."""

    row_consumption = RowConsumption.MAPPED
    display_name = "Prepare BBBC038 Segmentation Image"
    category = Category.CONVERSION
    environment = GENERAL_ENV

    class Inputs(IOModel):
        input_image: Annotated[Path, GUIMeta(display_name="Input image")]
        sample_id: Annotated[str, GUIMeta(display_name="Sample ID")]

    class Outputs(IOModel):
        segmentation_image: Annotated[str, GUIMeta(display_name="Segmentation image")]

    def process_row(self, arguments: Arguments, *, context: Any = None) -> Any:
        image = _as_2d_intensity(np.asarray(iio.imread(arguments.input_image)))
        output = Path(f"{arguments.sample_id}_segmentation_input.tif")
        if context is not None and getattr(context, "assets_dir", None):
            output = Path(context.assets_dir) / output
        output.parent.mkdir(parents=True, exist_ok=True)
        iio.imwrite(output, image.astype(np.float32, copy=False))
        return self.Outputs(segmentation_image=str(output))


class BenchmarkSegmentationMethod(ProcessingTool):
    """Benchmark one predicted label image against one reference label image."""

    row_consumption = RowConsumption.MAPPED
    display_name = "Benchmark Segmentation Method"
    category = Category.MEASUREMENT
    environment = GENERAL_ENV

    class Inputs(IOModel):
        method: Annotated[str, GUIMeta(display_name="Method")]
        input_image: Annotated[Path, GUIMeta(display_name="Input image")]
        predicted_label_image: Annotated[Path, GUIMeta(display_name="Predicted labels")]
        reference_label_image: Annotated[Path, GUIMeta(display_name="Reference labels")]

    class Outputs(IOModel):
        method: Annotated[str, GUIMeta(display_name="Method")]
        input_image: Annotated[str, GUIMeta(display_name="Input image")]
        predicted_label_image: Annotated[str, GUIMeta(display_name="Predicted labels")]
        reference_label_image: Annotated[str, GUIMeta(display_name="Reference labels")]
        overlay_image: Annotated[str, GUIMeta(display_name="Overlay preview")]
        predicted_label_count: Annotated[int, GUIMeta(display_name="Predicted labels")]
        reference_label_count: Annotated[int, GUIMeta(display_name="Reference labels")]
        true_positive_pixels: Annotated[int, GUIMeta(display_name="True positive pixels")]
        false_positive_pixels: Annotated[int, GUIMeta(display_name="False positive pixels")]
        false_negative_pixels: Annotated[int, GUIMeta(display_name="False negative pixels")]
        foreground_iou: Annotated[float, GUIMeta(display_name="Foreground IoU")]
        foreground_dice: Annotated[float, GUIMeta(display_name="Foreground Dice")]

    def process_row(self, arguments: Arguments, *, context: Any = None) -> Any:
        predicted = np.asarray(iio.imread(arguments.predicted_label_image))
        reference = np.asarray(iio.imread(arguments.reference_label_image))
        if predicted.shape != reference.shape:
            raise ValueError("predicted_label_image and reference_label_image must match.")

        overlay_path = _write_overlay(
            input_image=Path(arguments.input_image),
            predicted=predicted,
            reference=reference,
            method=str(arguments.method),
            context=context,
        )
        predicted_fg = predicted > 0
        reference_fg = reference > 0
        tp = int((predicted_fg & reference_fg).sum())
        fp = int((predicted_fg & ~reference_fg).sum())
        fn = int((~predicted_fg & reference_fg).sum())
        union = tp + fp + fn
        dice_denominator = (2 * tp) + fp + fn
        return self.Outputs(
            method=str(arguments.method),
            input_image=str(arguments.input_image),
            predicted_label_image=str(arguments.predicted_label_image),
            reference_label_image=str(arguments.reference_label_image),
            overlay_image=str(overlay_path),
            predicted_label_count=_label_count(predicted),
            reference_label_count=_label_count(reference),
            true_positive_pixels=tp,
            false_positive_pixels=fp,
            false_negative_pixels=fn,
            foreground_iou=float(tp / union) if union else 1.0,
            foreground_dice=float((2 * tp) / dice_denominator) if dice_denominator else 1.0,
        )


def _label_count(labels: np.ndarray) -> int:
    return int(np.count_nonzero(np.unique(labels) > 0))


def _as_2d_intensity(image: np.ndarray) -> np.ndarray:
    image = np.squeeze(image)
    if image.ndim == 2:
        return image.astype(np.float32, copy=False)
    if image.ndim == 3 and image.shape[-1] in {3, 4}:
        return image[..., :3].mean(axis=-1).astype(np.float32, copy=False)
    if image.ndim == 3:
        return image.max(axis=0).astype(np.float32, copy=False)
    raise ValueError(f"Expected a 2D, CYX, or YXC image, got shape {image.shape}.")


def _write_overlay(
    *,
    input_image: Path,
    predicted: np.ndarray,
    reference: np.ndarray,
    method: str,
    context: Any,
) -> Path:
    image = _as_2d_intensity(np.asarray(iio.imread(input_image), dtype=np.float32))
    image = image - float(image.min())
    max_value = float(image.max()) or 1.0
    base = np.clip(image / max_value, 0.0, 1.0)
    overlay = np.stack([base, base, base], axis=-1)
    overlay[reference > 0, 0] = 1.0
    overlay[predicted > 0, 1] = 1.0
    output = Path(f"{input_image.stem}_{method}_overlay.png")
    if context is not None and getattr(context, "assets_dir", None):
        output = Path(context.assets_dir) / output
    output.parent.mkdir(parents=True, exist_ok=True)
    iio.imwrite(output, (overlay * 255).astype(np.uint8))
    return output


def build_workflow(
    *,
    storage_path: str | Path = DEFAULT_STORAGE_PATH,
    engine: str = "wetlands",
    wetlands_config: dict | None = None,
) -> Workflow:
    """Build the BBBC038 segmentation benchmark workflow."""
    storage = Path(storage_path)
    wf = Workflow(
        name="bbbc038_segmentation_benchmark",
        display_name="BBBC038 Segmentation Benchmark",
        storage_path=str(storage),
        engine=engine,
        wetlands_config=wetlands_config,
    )
    with wf:
        data_dir = wf.input("data_dir", Path, default=Path(DEFAULT_DATA_DIR), id="input-data-dir")
        sample_glob = wf.input("sample_glob", str, default="*", id="input-sample-glob")
        samples = BBBC038Samples()(
            data_dir=data_dir,
            sample_glob=sample_glob,
            name="bbbc038_samples",
        )
        reference = BuildBBBC038ReferenceLabels()(
            mask_dir=samples["mask_dir"],
            sample_id=samples["sample_id"],
            name="build_reference_labels",
        )
        segmentation_input = PrepareBBBC038SegmentationImage()(
            input_image=samples["input_image"],
            sample_id=samples["sample_id"],
            name="prepare_segmentation_images",
        )

        cellpose3 = Cellpose3()(
            input_image=segmentation_input["segmentation_image"],
            model_type="nuclei",
            diameter=0.0,
            name="cellpose3_segmentation",
        )
        cellpose_sam = CellposeSAM()(
            input_image=segmentation_input["segmentation_image"],
            model_type="cpsam",
            diameter=0.0,
            name="cellpose_sam_segmentation",
        )
        stardist = StarDistSegmenter()(
            input_image=segmentation_input["segmentation_image"],
            model_name="2D_versatile_fluo",
            name="stardist_segmentation",
        )
        classical = ThresholdSegment()(
            input_image=segmentation_input["segmentation_image"],
            threshold=0.5,
            name="classical_threshold_segmentation",
        )

        cellpose3_metrics = BenchmarkSegmentationMethod()(
            method="cellpose3",
            input_image=samples["input_image"],
            predicted_label_image=cellpose3["mask"],
            reference_label_image=reference["reference_label_image"],
            name="benchmark_cellpose3",
        )
        cellpose_sam_metrics = BenchmarkSegmentationMethod()(
            method="cellpose_sam",
            input_image=samples["input_image"],
            predicted_label_image=cellpose_sam["mask"],
            reference_label_image=reference["reference_label_image"],
            name="benchmark_cellpose_sam",
        )
        stardist_metrics = BenchmarkSegmentationMethod()(
            method="stardist",
            input_image=samples["input_image"],
            predicted_label_image=stardist["mask"],
            reference_label_image=reference["reference_label_image"],
            name="benchmark_stardist",
        )
        classical_metrics = BenchmarkSegmentationMethod()(
            method="classical_threshold",
            input_image=samples["input_image"],
            predicted_label_image=classical["labels"],
            reference_label_image=reference["reference_label_image"],
            name="benchmark_classical_threshold",
        )
        benchmark = Concat()(
            cellpose3_metrics,
            cellpose_sam_metrics,
            stardist_metrics,
            classical_metrics,
            name="bbbc038_benchmark_metrics",
        )
        wf.output("method", benchmark["method"], id="output-method")
        wf.output("foreground_iou", benchmark["foreground_iou"], id="output-iou")
        wf.output("foreground_dice", benchmark["foreground_dice"], id="output-dice")
    return wf


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--data-dir",
        default=str(DEFAULT_DATA_DIR),
        help="Directory containing a BBBC038 stage1_train subset.",
    )
    parser.add_argument(
        "--storage-path",
        default=str(DEFAULT_STORAGE_PATH),
        help="Directory for workflow outputs.",
    )
    parser.add_argument("--sample-glob", default="*", help="Sample folder glob.")
    args = parser.parse_args()
    workflow = build_workflow(
        storage_path=args.storage_path,
    )
    print(workflow.compute(inputs={
        "data_dir": args.data_dir,
        "sample_glob": args.sample_glob,
    }).to_string(index=False))
