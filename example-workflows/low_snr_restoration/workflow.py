"""Low-SNR restoration workflow with CAREamics-style inference and metrics."""

from pathlib import Path
from typing import Annotated, Any

import imageio.v3 as iio
import numpy as np

from bioimageflow import Workflow
from bioimageflow.node import Node
from bioimageflow_core import (
    Arguments,
    Category,
    GENERAL_ENV,
    GUIMeta,
    IOModel,
    ProcessingTool,
)
from bioimageflow_restoration_tools import CAREamicsPredict, RestorationMetrics


class LowSNRFixture(ProcessingTool):
    """Write a clean image and a deterministic noisy observation."""

    display_name = "Low-SNR Fixture"
    category = Category.UTILITIES
    environment = GENERAL_ENV

    class Inputs(IOModel):
        output_dir: Annotated[Path, GUIMeta(display_name="Output directory")]
        seed: Annotated[int, GUIMeta(display_name="Seed")] = 7

    class Outputs(IOModel):
        clean_image: Annotated[Path, GUIMeta(display_name="Clean image")]
        degraded_image: Annotated[Path, GUIMeta(display_name="Degraded image")]

    def process_row(self, arguments: Arguments, *, context: Any = None) -> Any:
        output_dir = Path(arguments.output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        yy, xx = np.mgrid[0:64, 0:64]
        clean = np.zeros((64, 64), dtype=np.float32)
        clean[(yy - 30) ** 2 + (xx - 32) ** 2 <= 13**2] = 1.0
        clean[(yy - 20) ** 2 + (xx - 20) ** 2 <= 5**2] = 0.6
        rng = np.random.default_rng(int(arguments.seed))
        degraded = np.clip(clean + rng.normal(0.0, 0.20, clean.shape), 0.0, 1.0)
        clean_path = output_dir / "low_snr_clean.tif"
        degraded_path = output_dir / "low_snr_degraded.tif"
        iio.imwrite(clean_path, clean)
        iio.imwrite(degraded_path, degraded.astype(np.float32))
        return self.Outputs(clean_image=clean_path, degraded_image=degraded_path)


def build_workflow(
    storage_path: str = "./low_snr_restoration_results",
    engine: str = "wetlands",
    wetlands_config: dict | None = None,
) -> tuple[Workflow, Node]:
    """Build a low-SNR restoration evaluation workflow."""
    storage = Path(storage_path)
    wf = Workflow(
        storage_path=str(storage / "bif"),
        engine=engine,
        wetlands_config=wetlands_config,
    )
    with wf:
        fixture = LowSNRFixture()(output_dir=storage / "data", name="low_snr_fixture")
        restored = CAREamicsPredict()(
            input_image=fixture["degraded_image"],
            backend="baseline",
            method="tv_chambolle",
            weight=0.12,
            name="careamics_n2v_restoration",
        )
        metrics = RestorationMetrics()(
            clean_image=fixture["clean_image"],
            degraded_image=fixture["degraded_image"],
            restored_image=restored["output_image"],
            name="evaluate_restoration",
        )
    return wf, metrics


if __name__ == "__main__":
    workflow, terminal = build_workflow()
    print(workflow.compute(terminal).to_string(index=False))
