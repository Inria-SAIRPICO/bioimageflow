"""SAIRPICO PSF, denoising, deconvolution, and metric workflow."""

from pathlib import Path
from typing import Annotated, Any

import imageio.v3 as iio
import numpy as np
import pandas as pd

from bioimageflow import DataFrameTool, Workflow
from bioimageflow.node import Node
from bioimageflow_core import Arguments, Category, GENERAL_ENV, GUIMeta, IOModel, ProcessingTool
from bioimageflow_sairpico_tools import (
    GaussianPSF,
    MedianDenoising,
    RichardsonLucyDeconvolution,
)


class SairpicoInputFixture(ProcessingTool):
    """Write a tiny blurred input image for SAIRPICO examples."""

    display_name = "SAIRPICO Input Fixture"
    category = Category.UTILITIES
    environment = GENERAL_ENV

    class Inputs(IOModel):
        output_dir: Annotated[Path, GUIMeta(display_name="Output directory")]

    class Outputs(IOModel):
        input_image: Annotated[Path, GUIMeta(display_name="Input image")]

    def process_row(self, arguments: Arguments, *, context: Any = None) -> Any:
        output_dir = Path(arguments.output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        yy, xx = np.mgrid[0:32, 0:32]
        image = np.exp(-(((yy - 16) ** 2 + (xx - 16) ** 2) / 42.0)).astype(np.float32)
        image += 0.08 * np.exp(-(((yy - 10) ** 2 + (xx - 23) ** 2) / 8.0)).astype(np.float32)
        path = output_dir / "synthetic_sairpico_input.tif"
        iio.imwrite(path, image)
        return self.Outputs(input_image=path)


class DeconvolutionMetrics(DataFrameTool):
    """Compute simple sharpness and residual-noise metrics."""

    display_name = "Deconvolution Metrics"
    category = Category.MEASUREMENT

    class Inputs(IOModel):
        pass

    class Outputs(IOModel):
        input_image: Annotated[str, GUIMeta(display_name="Input image")]
        psf_image: Annotated[str, GUIMeta(display_name="PSF image")]
        denoised_image: Annotated[str, GUIMeta(display_name="Denoised image")]
        deconvolved_image: Annotated[str, GUIMeta(display_name="Deconvolved image")]
        input_sharpness: Annotated[float, GUIMeta(display_name="Input sharpness")]
        deconvolved_sharpness: Annotated[float, GUIMeta(display_name="Deconvolved sharpness")]
        denoised_residual_noise: Annotated[float, GUIMeta(display_name="Denoised residual noise")]

    def merge_dataframes(self, dfs: list[Any], arguments: Any) -> pd.DataFrame:
        if len(dfs) != 4:
            raise ValueError("DeconvolutionMetrics expects fixture, psf, denoised, and deconvolved tables.")
        fixture, psf, denoised_table, deconvolved_table = (pd.DataFrame(df) for df in dfs)
        rows = []
        for index in range(min(len(fixture), len(psf), len(denoised_table), len(deconvolved_table))):
            input_path = str(fixture.iloc[index]["input_image"])
            psf_path = str(psf.iloc[index]["output_image"])
            denoised_path = str(denoised_table.iloc[index]["output_image"])
            deconvolved_path = str(deconvolved_table.iloc[index]["output_image"])
            input_image = iio.imread(input_path).astype(np.float32)
            denoised = iio.imread(denoised_path).astype(np.float32)
            deconvolved = iio.imread(deconvolved_path).astype(np.float32)
            rows.append(
                {
                    "input_image": input_path,
                    "psf_image": psf_path,
                    "denoised_image": denoised_path,
                    "deconvolved_image": deconvolved_path,
                    "input_sharpness": _sharpness(input_image),
                    "deconvolved_sharpness": _sharpness(deconvolved),
                    "denoised_residual_noise": float(np.std(denoised - input_image)),
                }
            )
        return pd.DataFrame(rows)

    def transform(self, df: Any, arguments: Any) -> pd.DataFrame:
        return pd.DataFrame(df)


def _sharpness(image: np.ndarray) -> float:
    gy, gx = np.gradient(image.astype(np.float32))
    return float(np.mean(gx**2 + gy**2))


def build_workflow(
    storage_path: str = "./sairpico_deconvolution_results",
    engine: str = "wetlands",
    wetlands_config: dict | None = None,
) -> tuple[Workflow, Node]:
    """Build a SAIRPICO deconvolution workflow with metrics."""
    storage = Path(storage_path)
    wf = Workflow(
        storage_path=str(storage / "bif"),
        engine=engine,
        wetlands_config=wetlands_config,
    )
    with wf:
        fixture = SairpicoInputFixture()(output_dir=storage / "data", name="sairpico_input")
        psf = GaussianPSF()(
            width=16,
            height=16,
            depth=5,
            sigmaxy=1.2,
            sigmaz=1.6,
            name="sairpico_gaussian_psf",
        )
        denoised = MedianDenoising()(
            input_image=fixture["input_image"],
            denoising_type="2D",
            radius_x=1,
            radius_y=1,
            padding=True,
            name="sairpico_median_denoise",
        )
        deconvolved = RichardsonLucyDeconvolution()(
            input_image=denoised["output_image"],
            deconvolution_type="2D",
            sigma=1.2,
            niter=3,
            regularization_lambda=0.01,
            padding=True,
            name="sairpico_richardson_lucy",
        )
        metrics = DeconvolutionMetrics()(
            fixture,
            psf,
            denoised,
            deconvolved,
            name="sairpico_deconvolution_metrics",
        )
    return wf, metrics


if __name__ == "__main__":
    workflow, terminal = build_workflow()
    print(workflow.compute(terminal).to_string(index=False))
