"""Dependency-light restoration baseline tools."""

from pathlib import Path
from typing import Annotated, Any

from bioimageflow_core import (
    Arguments,
    Category,
    Connectable,
    GENERAL_ENV,
    GUIMeta,
    ImageSpec,
    IOModel,
    Layout,
    ProcessingTool,
    Semantic,
    Template,
)

from ._filters import gaussian_blur


IntensityImage = Annotated[
    Path,
    ImageSpec(semantics={Semantic.INTENSITY}, layouts={Layout.PLANAR}),
    GUIMeta(
        display_name="Input image",
        description="2D intensity image.",
        connectable=Connectable.BY_DEFAULT,
    ),
]

OutputImage = Annotated[
    Path,
    ImageSpec(semantics={Semantic.INTENSITY}, layouts={Layout.PLANAR}),
    GUIMeta(display_name="Output image"),
]


def _write_image(path: Path, image: Any) -> Path:
    import imageio.v3 as iio
    import numpy as np

    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    iio.imwrite(output, image.astype(np.float32, copy=False))
    return output


def _argument(arguments: Arguments, name: str, default: Any) -> Any:
    return getattr(arguments, name, default)


def _median_filter(image: Any, radius: int) -> Any:
    import numpy as np

    try:
        from scipy.ndimage import median_filter
    except ImportError:
        radius = max(0, int(radius))
        padded = np.pad(image, radius, mode="edge")
        result = np.empty_like(image, dtype=np.float32)
        size = radius * 2 + 1
        for y in range(image.shape[0]):
            for x in range(image.shape[1]):
                result[y, x] = np.median(padded[y : y + size, x : x + size])
        return result
    return median_filter(image, size=max(1, int(radius) * 2 + 1)).astype(np.float32)


def _convolve2d(image: Any, kernel: Any) -> Any:
    import numpy as np

    ky, kx = kernel.shape
    py = ky // 2
    px = kx // 2
    padded = np.pad(image, ((py, py), (px, px)), mode="edge")
    result = np.zeros_like(image, dtype=np.float32)
    for y in range(image.shape[0]):
        for x in range(image.shape[1]):
            result[y, x] = float(np.sum(padded[y : y + ky, x : x + kx] * kernel))
    return result


def _default_psf(size: int = 5, sigma: float = 1.0) -> Any:
    import numpy as np

    radius = size // 2
    yy, xx = np.mgrid[-radius : radius + 1, -radius : radius + 1]
    psf = np.exp(-(xx**2 + yy**2) / (2.0 * sigma**2)).astype(np.float32)
    return psf / psf.sum()


def _richardson_lucy(image: Any, psf: Any, iterations: int, clip: bool) -> Any:
    import numpy as np

    image = np.maximum(image.astype(np.float32, copy=False), 0.0)
    psf = np.maximum(psf.astype(np.float32, copy=False), 0.0)
    psf = psf / psf.sum() if float(psf.sum()) > 0 else _default_psf()
    psf_mirror = psf[::-1, ::-1]
    estimate = np.full_like(image, max(float(image.mean()), 1e-6), dtype=np.float32)
    for _ in range(max(1, int(iterations))):
        blurred = _convolve2d(estimate, psf)
        relative_blur = image / np.maximum(blurred, 1e-6)
        estimate *= _convolve2d(relative_blur, psf_mirror)
        estimate = np.nan_to_num(estimate, nan=0.0, posinf=0.0, neginf=0.0)
    return np.clip(estimate, 0.0, 1.0) if clip else estimate


class GaussianDenoise(ProcessingTool):
    """Apply Gaussian smoothing to a scalar image."""

    display_name = "Gaussian Denoise"
    documentation = "Simple Gaussian denoising baseline."
    category = Category.RESTORATION
    tags = ["restoration", "denoise", "gaussian"]
    environment = GENERAL_ENV

    class Inputs(IOModel):
        input_image: IntensityImage
        sigma: Annotated[float, GUIMeta(min=0.2, max=20.0, step=0.1)] = 1.0

    class Outputs(IOModel):
        output_image: OutputImage = Template("{input_image.stem}_gaussian.tif")

    def process_row(self, arguments: Arguments, *, context: Any = None) -> Any:
        import imageio.v3 as iio

        image = iio.imread(arguments.input_image).astype("float32")
        output = _write_image(arguments.output_image, gaussian_blur(image, arguments.sigma))
        return self.Outputs(output_image=output)


class MedianDenoise(ProcessingTool):
    """Apply median denoising to a scalar image."""

    display_name = "Median Denoise"
    documentation = "Dependency-light median denoising baseline."
    category = Category.RESTORATION
    tags = ["restoration", "denoise", "median"]
    environment = GENERAL_ENV

    class Inputs(IOModel):
        input_image: IntensityImage
        radius: Annotated[int, GUIMeta(min=0, step=1)] = 1

    class Outputs(IOModel):
        output_image: OutputImage = Template("{input_image.stem}_median.tif")

    def process_row(self, arguments: Arguments, *, context: Any = None) -> Any:
        import imageio.v3 as iio

        image = iio.imread(arguments.input_image).astype("float32")
        output = _write_image(arguments.output_image, _median_filter(image, arguments.radius))
        return self.Outputs(output_image=output)


class BackgroundSubtract(ProcessingTool):
    """Subtract a smoothed background estimate from an image."""

    display_name = "Background Subtract"
    documentation = "Subtract a Gaussian-smoothed background estimate."
    category = Category.RESTORATION
    tags = ["restoration", "background", "baseline"]
    environment = GENERAL_ENV

    class Inputs(IOModel):
        input_image: IntensityImage
        sigma: Annotated[float, GUIMeta(min=0.2, max=50.0, step=0.1)] = 10.0
        preserve_range: bool = True

    class Outputs(IOModel):
        output_image: OutputImage = Template("{input_image.stem}_background_subtracted.tif")

    def process_row(self, arguments: Arguments, *, context: Any = None) -> Any:
        import imageio.v3 as iio
        import numpy as np

        image = iio.imread(arguments.input_image).astype(np.float32)
        background = gaussian_blur(image, arguments.sigma)
        result = image - background
        if _argument(arguments, "preserve_range", True):
            result = result - float(result.min())
        output = _write_image(arguments.output_image, result)
        return self.Outputs(output_image=output)


class UnsharpMask(ProcessingTool):
    """Sharpen an image by adding high-frequency residuals."""

    display_name = "Unsharp Mask"
    documentation = "Sharpening baseline with clear radius and amount parameters."
    category = Category.RESTORATION
    tags = ["restoration", "sharpen", "unsharp"]
    environment = GENERAL_ENV

    class Inputs(IOModel):
        input_image: IntensityImage
        sigma: Annotated[float, GUIMeta(min=0.2, max=20.0, step=0.1)] = 1.0
        amount: Annotated[float, GUIMeta(min=0.0, max=5.0, step=0.1)] = 1.0

    class Outputs(IOModel):
        output_image: OutputImage = Template("{input_image.stem}_unsharp.tif")

    def process_row(self, arguments: Arguments, *, context: Any = None) -> Any:
        import imageio.v3 as iio

        image = iio.imread(arguments.input_image).astype("float32")
        low = gaussian_blur(image, arguments.sigma)
        sharpened = image + float(arguments.amount) * (image - low)
        output = _write_image(arguments.output_image, sharpened)
        return self.Outputs(output_image=output)


class RichardsonLucyRestoration(ProcessingTool):
    """Apply Richardson-Lucy deconvolution with a provided or default PSF."""

    display_name = "Richardson-Lucy Restoration"
    documentation = "scikit-image-style Richardson-Lucy deconvolution baseline."
    category = Category.DECONVOLUTION
    tags = ["restoration", "deconvolution", "richardson-lucy"]
    environment = GENERAL_ENV

    class Inputs(IOModel):
        input_image: IntensityImage
        psf_image: Annotated[
            Path,
            ImageSpec(semantics={Semantic.INTENSITY}, layouts={Layout.PLANAR}),
            GUIMeta("PSF image", connectable=Connectable.NOT_BY_DEFAULT),
        ] = None
        iterations: Annotated[int, GUIMeta(min=1, step=1)] = 10
        clip: bool = False

    class Outputs(IOModel):
        output_image: OutputImage = Template("{input_image.stem}_richardson_lucy.tif")

    def process_row(self, arguments: Arguments, *, context: Any = None) -> Any:
        import imageio.v3 as iio

        image = iio.imread(arguments.input_image).astype("float32")
        psf = (
            iio.imread(arguments.psf_image).astype("float32")
            if arguments.psf_image is not None
            else _default_psf()
        )
        output = _write_image(
            arguments.output_image,
            _richardson_lucy(
                image,
                psf,
                _argument(arguments, "iterations", 10),
                _argument(arguments, "clip", False),
            ),
        )
        return self.Outputs(output_image=output)
