"""Low-SNR restoration workflow with CAREamics-style inference and metrics."""

import argparse
from pathlib import Path
from typing import Annotated, Any

import imageio.v3 as iio
import numpy as np

from bioimageflow import Workflow
from bioimageflow.node import Node
from bioimageflow_common_tools import JoinOnColumn
from bioimageflow_core import (
    Arguments,
    Category,
    GENERAL_ENV,
    GUIMeta,
    IOModel,
    ProcessingTool,
)
from bioimageflow_restoration_tools import CAREamicsPredict, RestorationMetrics


class RestorationPreview(ProcessingTool):
    """Create a side-by-side preview of clean, degraded, and restored images."""

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
    clean_image: str | None = None,
    degraded_image: str | None = None,
    checkpoint: str | None = None,
    storage_path: str = "./low_snr_restoration_results",
    engine: str = "wetlands",
    wetlands_config: dict | None = None,
) -> tuple[Workflow, Node]:
    """Build a low-SNR restoration evaluation workflow."""
    if clean_image is None or degraded_image is None or checkpoint is None:
        raise ValueError("build_workflow requires clean_image, degraded_image, and checkpoint.")
    storage = Path(storage_path)
    wf = Workflow(
        storage_path=str(storage / "bif"),
        engine=engine,
        wetlands_config=wetlands_config,
    )
    with wf:
        restored = CAREamicsPredict()(
            input_image=degraded_image,
            checkpoint=Path(checkpoint),
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
    return wf, results


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--clean-image", required=True, help="Clean or high-SNR reference image.")
    parser.add_argument("--degraded-image", required=True, help="Low-SNR image to restore.")
    parser.add_argument("--checkpoint", required=True, help="CAREamics checkpoint.")
    parser.add_argument(
        "--storage-path",
        default="./low_snr_restoration_results",
        help="Directory for workflow outputs.",
    )
    args = parser.parse_args()
    workflow, terminal = build_workflow(
        clean_image=args.clean_image,
        degraded_image=args.degraded_image,
        checkpoint=args.checkpoint,
        storage_path=args.storage_path,
    )
    print(workflow.compute(terminal).to_string(index=False))
