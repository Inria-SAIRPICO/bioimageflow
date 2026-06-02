from pathlib import Path

import imageio.v3 as iio
import numpy as np
import pandas as pd
import pytest

from bioimageflow import Workflow
from bioimageflow_core import Arguments
from bioimageflow_restoration_tools import (
    BackgroundSubtract,
    BenchmarkRestoration,
    GaussianDenoise,
    MedianDenoise,
    RestoreImage,
    RichardsonLucyRestoration,
    UnsharpMask,
)

pytestmark = pytest.mark.package_tools


def test_restore_image_improves_noisy_synthetic_image(tmp_path: Path) -> None:
    clean = np.zeros((48, 48), dtype=np.float32)
    clean[14:34, 14:34] = 1.0
    rng = np.random.default_rng(42)
    noisy = np.clip(clean + rng.normal(0.0, 0.18, clean.shape), 0.0, 1.0)
    clean_path = tmp_path / "clean.tif"
    noisy_path = tmp_path / "noisy.tif"
    iio.imwrite(clean_path, clean)
    iio.imwrite(noisy_path, noisy.astype(np.float32))

    result = RestoreImage().process_row(
        Arguments(
            input_image=str(noisy_path),
            method="tv_chambolle",
            weight=0.12,
            output_image=str(tmp_path / "restored.tif"),
        )
    )

    restored = iio.imread(result.output_image).astype(np.float32)
    assert restored.shape == noisy.shape
    assert np.mean((restored - clean) ** 2) < np.mean((noisy - clean) ** 2)


def test_benchmark_restoration_writes_metrics(tmp_path: Path) -> None:
    result = BenchmarkRestoration().process_row(
        Arguments(
            image_size=48,
            noise_sigma=0.12,
            blur_sigma=1.0,
            seed=7,
            clean_image=str(tmp_path / "declared" / "clean_custom.tif"),
            degraded_image=str(tmp_path / "declared" / "degraded_custom.tif"),
            restored_image=str(tmp_path / "declared" / "restored_custom.tif"),
            metrics_csv=str(tmp_path / "declared" / "metrics_custom.csv"),
        )
    )

    assert Path(result.clean_image) == tmp_path / "declared" / "clean_custom.tif"
    assert Path(result.degraded_image) == tmp_path / "declared" / "degraded_custom.tif"
    assert Path(result.restored_image) == tmp_path / "declared" / "restored_custom.tif"
    assert Path(result.metrics_csv) == tmp_path / "declared" / "metrics_custom.csv"
    metrics = pd.read_csv(result.metrics_csv)
    assert {"degraded_psnr", "restored_psnr", "mse_degraded", "mse_restored"} <= set(
        metrics.columns
    )
    assert metrics.loc[0, "restored_psnr"] > metrics.loc[0, "degraded_psnr"]
    assert Path(result.restored_image).exists()


def test_restoration_workflow_graph_runs(tmp_path: Path) -> None:
    image = np.zeros((32, 32), dtype=np.float32)
    image[8:24, 8:24] = 1.0
    image_path = tmp_path / "image.tif"
    iio.imwrite(image_path, image)

    with Workflow(storage_path=str(tmp_path / "bif")) as wf:
        restored = RestoreImage()(input_image=image_path, name="restore")
        result = wf.compute(restored)

    assert Path(result.iloc[0]["output_image"]).exists()


def test_gaussian_and_median_denoise_write_smoothed_images(tmp_path: Path) -> None:
    image = np.zeros((21, 21), dtype=np.float32)
    image[10, 10] = 10.0
    image[2, 2] = 8.0
    image_path = tmp_path / "impulse.tif"
    iio.imwrite(image_path, image)

    gaussian = GaussianDenoise().process_row(
        Arguments(input_image=image_path, sigma=1.0, output_image=tmp_path / "gaussian.tif")
    )
    median = MedianDenoise().process_row(
        Arguments(input_image=image_path, radius=1, output_image=tmp_path / "median.tif")
    )

    gaussian_image = iio.imread(gaussian.output_image)
    median_image = iio.imread(median.output_image)
    assert 0.0 < gaussian_image[10, 10] < image[10, 10]
    assert median_image[2, 2] == 0.0


def test_background_subtract_and_unsharp_mask_adjust_contrast(tmp_path: Path) -> None:
    image = np.ones((24, 24), dtype=np.float32) * 5.0
    image[8:16, 8:16] = 8.0
    image_path = tmp_path / "contrast.tif"
    iio.imwrite(image_path, image)

    background = BackgroundSubtract().process_row(
        Arguments(input_image=image_path, sigma=3.0, output_image=tmp_path / "background.tif")
    )
    sharpened = UnsharpMask().process_row(
        Arguments(
            input_image=image_path,
            sigma=1.0,
            amount=1.0,
            output_image=tmp_path / "unsharp.tif",
        )
    )

    background_image = iio.imread(background.output_image)
    sharpened_image = iio.imread(sharpened.output_image)
    assert background_image[12, 12] > background_image[0, 0]
    assert sharpened_image[8, 8] > image[8, 8]


def test_richardson_lucy_restoration_accepts_psf_and_preserves_shape(tmp_path: Path) -> None:
    image = np.zeros((15, 15), dtype=np.float32)
    image[7, 7] = 1.0
    psf = np.ones((3, 3), dtype=np.float32) / 9.0
    image_path = tmp_path / "blurred.tif"
    psf_path = tmp_path / "psf.tif"
    iio.imwrite(image_path, image)
    iio.imwrite(psf_path, psf)

    result = RichardsonLucyRestoration().process_row(
        Arguments(
            input_image=image_path,
            psf_image=psf_path,
            iterations=3,
            output_image=tmp_path / "rl.tif",
        )
    )

    restored = iio.imread(result.output_image)
    assert restored.shape == image.shape
    assert np.isfinite(restored).all()
