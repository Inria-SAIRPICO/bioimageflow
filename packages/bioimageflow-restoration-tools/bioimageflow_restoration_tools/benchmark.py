"""Synthetic blur/noise restoration benchmark workflow tool."""

from pathlib import Path
from typing import Annotated, Any
import csv

from bioimageflow_core import (
    Arguments,
    Category,
    GENERAL_ENV,
    GUIMeta,
    ImageSpec,
    IOModel,
    Layout,
    ProcessingTool,
    Semantic,
    Template,
)


def _gaussian_kernel1d(sigma: float) -> "Any":
    import numpy as np

    sigma = max(float(sigma), 0.2)
    radius = max(1, int(3.0 * sigma + 0.5))
    x = np.arange(-radius, radius + 1, dtype=np.float32)
    kernel = np.exp(-(x**2) / (2.0 * sigma**2))
    return kernel / kernel.sum()


def gaussian_blur(image: "Any", sigma: float) -> "Any":
    import numpy as np

    kernel = _gaussian_kernel1d(sigma)
    result = image.astype(np.float32, copy=False)
    for axis in range(result.ndim):
        pad = [(0, 0)] * result.ndim
        pad[axis] = (len(kernel) // 2, len(kernel) // 2)
        padded = np.pad(result, pad, mode="edge")
        result = np.apply_along_axis(
            lambda line: np.convolve(line, kernel, mode="valid"), axis, padded
        )
    return result.astype(np.float32)


def restore_array(image: "Any", weight: float = 0.12) -> "Any":
    try:
        from skimage.restoration import denoise_tv_chambolle
    except ImportError:
        return gaussian_blur(image, max(float(weight) * 4.0, 0.4))
    return denoise_tv_chambolle(image, weight=float(weight))


def _mse(reference: "Any", image: "Any") -> float:
    import numpy as np

    return float(np.mean((reference.astype(np.float32) - image.astype(np.float32)) ** 2))


def _psnr(reference: "Any", image: "Any") -> float:
    import math

    mse = _mse(reference, image)
    if mse == 0:
        return float("inf")
    return 20.0 * math.log10(1.0) - 10.0 * math.log10(mse)


class BenchmarkRestoration(ProcessingTool):
    """Generate a synthetic blurred/noisy image, restore it, and write metrics."""

    display_name = "Benchmark Restoration"
    documentation = "Create a synthetic blur/noise restoration benchmark and PSNR table."
    category = Category.RESTORATION
    tags = ["restoration", "benchmark", "synthetic"]
    environment = GENERAL_ENV

    class Inputs(IOModel):
        image_size: int = 64
        noise_sigma: float = 0.12
        blur_sigma: float = 1.0
        seed: int = 0

    class Outputs(IOModel):
        clean_image: Annotated[
            Path,
            ImageSpec(semantics={Semantic.INTENSITY}, layouts={Layout.PLANAR}),
            GUIMeta(display_name="Clean image"),
        ] = Template("clean.tif")
        degraded_image: Annotated[
            Path,
            ImageSpec(semantics={Semantic.INTENSITY}, layouts={Layout.PLANAR}),
            GUIMeta(display_name="Degraded image"),
        ] = Template("degraded.tif")
        restored_image: Annotated[
            Path,
            ImageSpec(semantics={Semantic.INTENSITY}, layouts={Layout.PLANAR}),
            GUIMeta(display_name="Restored image"),
        ] = Template("restored.tif")
        metrics_csv: Annotated[Path, GUIMeta(display_name="Benchmark metrics")] = (
            Template("metrics.csv")
        )
        restored_psnr: Annotated[float, GUIMeta(display_name="Restored PSNR")]

    def process_row(self, arguments: Arguments, *, context: Any = None) -> Any:
        import imageio.v3 as iio
        import numpy as np

        size = int(arguments.image_size)
        rng = np.random.default_rng(int(arguments.seed))
        yy, xx = np.mgrid[0:size, 0:size]
        clean = np.zeros((size, size), dtype=np.float32)
        clean[((yy - size * 0.35) ** 2 + (xx - size * 0.35) ** 2) < (size * 0.16) ** 2] = 1.0
        clean[size // 2 : size // 2 + size // 5, size // 3 : size // 3 + size // 3] = 0.65
        degraded = gaussian_blur(clean, float(arguments.blur_sigma))
        degraded = np.clip(
            degraded + rng.normal(0.0, float(arguments.noise_sigma), clean.shape),
            0.0,
            1.0,
        ).astype(np.float32)
        restored = restore_array(degraded, weight=0.12)

        clean_path = Path(arguments.clean_image)
        degraded_path = Path(arguments.degraded_image)
        restored_path = Path(arguments.restored_image)
        metrics_path = Path(arguments.metrics_csv)
        for path in [clean_path, degraded_path, restored_path, metrics_path]:
            path.parent.mkdir(parents=True, exist_ok=True)
        iio.imwrite(clean_path, clean)
        iio.imwrite(degraded_path, degraded)
        iio.imwrite(restored_path, restored.astype(np.float32))

        metrics = {
            "mse_degraded": _mse(clean, degraded),
            "mse_restored": _mse(clean, restored),
            "degraded_psnr": _psnr(clean, degraded),
            "restored_psnr": _psnr(clean, restored),
        }
        with metrics_path.open("w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(metrics))
            writer.writeheader()
            writer.writerow(metrics)
        return self.Outputs(
            clean_image=clean_path,
            degraded_image=degraded_path,
            restored_image=restored_path,
            metrics_csv=metrics_path,
            restored_psnr=float(metrics["restored_psnr"]),
        )
