"""SAIRPICO PSF, denoising, deconvolution, and metric workflow."""

import argparse
from pathlib import Path
from typing import Annotated, Any

import imageio.v3 as iio
import numpy as np
import pandas as pd

from bioimageflow import DataFrameTool, Workflow
from bioimageflow_common_tools import CrossJoin
from bioimageflow_core import Category, GUIMeta, IOModel
from bioimageflow_sairpico_tools import (
    GaussianPSF,
    MedianDenoising,
    RichardsonLucyDeconvolution,
)

DEFAULT_STORAGE_PATH = Path(__file__).resolve().parent / "results"


class DeconvolutionMetrics(DataFrameTool):
    """Compute simple sharpness and residual-noise metrics."""

    display_name = "Deconvolution Metrics"
    category = Category.MEASUREMENT

    class Inputs(IOModel):
        input_image: Annotated[Path, GUIMeta(display_name="Input image")]

    class Outputs(IOModel):
        input_image: Annotated[str, GUIMeta(display_name="Input image")]
        psf_image: Annotated[str, GUIMeta(display_name="PSF image")]
        denoised_image: Annotated[str, GUIMeta(display_name="Denoised image")]
        deconvolved_image: Annotated[str, GUIMeta(display_name="Deconvolved image")]
        input_sharpness: Annotated[float, GUIMeta(display_name="Input sharpness")]
        deconvolved_sharpness: Annotated[float, GUIMeta(display_name="Deconvolved sharpness")]
        denoised_residual_noise: Annotated[float, GUIMeta(display_name="Denoised residual noise")]

    def merge_dataframes(self, dfs: list[Any], arguments: Any) -> pd.DataFrame:
        if len(dfs) != 2:
            raise ValueError("DeconvolutionMetrics expects combined PSF/denoised and deconvolved tables.")
        psf_denoised, deconvolved_table = (pd.DataFrame(df) for df in dfs)
        rows = []
        for index in range(min(len(psf_denoised), len(deconvolved_table))):
            input_path = str(arguments.input_image)
            psf_path = str(psf_denoised.iloc[index]["output_image_left"])
            denoised_path = str(psf_denoised.iloc[index]["output_image_right"])
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
    *,
    storage_path: str | Path = DEFAULT_STORAGE_PATH,
    engine: str = "wetlands",
    wetlands_config: dict | None = None,
) -> Workflow:
    """Build a SAIRPICO deconvolution workflow with metrics."""
    storage = Path(storage_path)
    wf = Workflow(
        name="sairpico_deconvolution",
        display_name="SAIRPICO Deconvolution",
        storage_path=str(storage),
        engine=engine,
        wetlands_config=wetlands_config,
    )
    with wf:
        input_image = wf.input("input_image", Path, id="input-image")
        psf = GaussianPSF()(
            width=16,
            height=16,
            depth=5,
            sigmaxy=1.2,
            sigmaz=1.6,
            name="sairpico_gaussian_psf",
        )
        denoised = MedianDenoising()(
            input_image=input_image,
            denoising_type="2D",
            radius_x=1,
            radius_y=1,
            padding=True,
            name="sairpico_median_denoise",
        )
        psf_denoised = CrossJoin()(
            psf,
            denoised,
            name="sairpico_psf_denoised_inputs",
        )
        deconvolved = RichardsonLucyDeconvolution()(
            input_image=psf_denoised["output_image_right"],
            deconvolution_type="3D",
            sigma=1.2,
            psf_image=psf_denoised["output_image_left"],
            niter=3,
            regularization_lambda=0.01,
            padding=True,
            name="sairpico_richardson_lucy",
        )
        metrics = DeconvolutionMetrics()(
            psf_denoised,
            deconvolved,
            input_image=input_image,
            name="sairpico_deconvolution_metrics",
        )
        wf.output("deconvolved_image", metrics["deconvolved_image"], id="output-deconvolved-image")
        wf.output("deconvolved_sharpness", metrics["deconvolved_sharpness"], id="output-sharpness")
    return wf


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-image", required=True, help="Microscopy crop to denoise and deconvolve.")
    parser.add_argument(
        "--storage-path",
        default=str(DEFAULT_STORAGE_PATH),
        help="Directory for workflow outputs.",
    )
    args = parser.parse_args()
    workflow = build_workflow(storage_path=args.storage_path)
    print(workflow.compute(inputs={"input_image": args.input_image}).to_string(index=False))
