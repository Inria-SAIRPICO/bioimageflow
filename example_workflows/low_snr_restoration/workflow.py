"""Low-SNR restoration workflow with CAREamics-style inference and metrics."""

import argparse
from pathlib import Path
from typing import Annotated, Any

import imageio.v3 as iio
import numpy as np

from bioimageflow import Workflow
from bioimageflow_common_tools import JoinOnColumn
from bioimageflow_core import (
    Arguments,
    Category,
    GENERAL_ENV,
    GUIMeta,
    IOModel,
    ProcessingTool,
    RowConsumption,
)
from bioimageflow_restoration_tools import CAREamicsPredict, RestorationMetrics

DEFAULT_STORAGE_PATH = Path(__file__).resolve().parent / "results"


class RestorationPreview(ProcessingTool):
    """Create a side-by-side preview of clean, degraded, and restored images."""

    row_consumption = RowConsumption.MAPPED
    display_name = "Restoration Preview"
    category = Category.UTILITIES
    environment = GENERAL_ENV

    class Inputs(IOModel):
        clean_image: Annotated[Path, GUIMeta(display_name="Clean image")]
        degraded_image: Annotated[Path, GUIMeta(display_name="Degraded image")]
        restored_image: Annotated[Path, GUIMeta(display_name="Restored image")]

    class Outputs(IOModel):
        restored_image: Annotated[str, GUIMeta(display_name="Restored image")]
        preview_image: Annotated[str, GUIMeta(display_name="Preview image")]

    def process_row(self, arguments: Arguments, *, context: Any = None) -> Any:
        clean = _normalize(iio.imread(arguments.clean_image).astype(np.float32))
        degraded = _normalize(iio.imread(arguments.degraded_image).astype(np.float32))
        restored = _normalize(iio.imread(arguments.restored_image).astype(np.float32))
        if clean.shape != degraded.shape or clean.shape != restored.shape:
            raise ValueError("clean_image, degraded_image, and restored_image must match.")
        separator = np.ones((clean.shape[0], 2), dtype=np.float32)
        preview = np.concatenate([clean, separator, degraded, separator, restored], axis=1)
        output = Path(f"{Path(arguments.restored_image).stem}_preview.png")
        if context is not None and getattr(context, "assets_dir", None):
            output = Path(context.assets_dir) / output
        output.parent.mkdir(parents=True, exist_ok=True)
        iio.imwrite(output, (np.clip(preview, 0.0, 1.0) * 255).astype(np.uint8))
        return self.Outputs(
            restored_image=str(arguments.restored_image),
            preview_image=str(output),
        )


def _normalize(image: np.ndarray) -> np.ndarray:
    image = np.squeeze(image).astype(np.float32, copy=False)
    if image.ndim != 2:
        raise ValueError(f"Expected a 2D image, got shape {image.shape}.")
    image = image - float(image.min())
    return image / (float(image.max()) or 1.0)


def build_workflow(
    *,
    storage_path: str | Path = DEFAULT_STORAGE_PATH,
    engine: str = "wetlands",
    wetlands_config: dict | None = None,
) -> Workflow:
    """Build a low-SNR restoration evaluation workflow."""
    storage = Path(storage_path)
    wf = Workflow(
        name="low_snr_restoration",
        display_name="Low-SNR Restoration",
        storage_path=str(storage),
        engine=engine,
        wetlands_config=wetlands_config,
    )
    with wf:
        clean_image = wf.input("clean_image", Path, id="input-clean-image")
        degraded_image = wf.input("degraded_image", Path, id="input-degraded-image")
        checkpoint = wf.input("checkpoint", Path, id="input-checkpoint")
        restored = CAREamicsPredict()(
            input_image=degraded_image,
            checkpoint=checkpoint,
            name="careamics_n2v_restoration",
        )
        metrics = RestorationMetrics()(
            clean_image=clean_image,
            degraded_image=degraded_image,
            restored_image=restored["output_image"],
            name="evaluate_restoration",
        )
        preview = RestorationPreview()(
            clean_image=clean_image,
            degraded_image=degraded_image,
            restored_image=restored["output_image"],
            name="restoration_preview",
        )
        results = JoinOnColumn()(
            metrics,
            preview,
            join_column="restored_image",
            name="restoration_results",
        )
        wf.output("restored_image", results["restored_image"], id="output-restored-image")
        wf.output("preview_image", results["preview_image"], id="output-preview-image")
    return wf


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--clean-image", required=True, help="Clean or high-SNR reference image.")
    parser.add_argument("--degraded-image", required=True, help="Low-SNR image to restore.")
    parser.add_argument("--checkpoint", required=True, help="CAREamics checkpoint.")
    parser.add_argument(
        "--storage-path",
        default=str(DEFAULT_STORAGE_PATH),
        help="Directory for workflow outputs.",
    )
    args = parser.parse_args()
    workflow = build_workflow(storage_path=args.storage_path)
    print(workflow.compute(inputs={
        "clean_image": args.clean_image,
        "degraded_image": args.degraded_image,
        "checkpoint": args.checkpoint,
    }).to_string(index=False))
