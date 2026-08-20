"""Classical image-restoration tools backed by SciPy and scikit-image."""

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
    RowConsumption,
    Semantic,
    Template,
)

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


def _read_image(path: Path, *, name: str = "input_image") -> Any:
    import imageio.v3 as iio
    import numpy as np

    image = iio.imread(path).astype(np.float32)
    if image.ndim != 2:
        raise ValueError(f"{name} must be a 2D image; got shape {image.shape}.")
    if image.size == 0:
        raise ValueError(f"{name} must not be empty.")
    if not np.isfinite(image).all():
        raise ValueError(f"{name} must contain only finite values.")
    return image


def _positive_float(value: Any, *, name: str) -> float:
    import math

    result = float(value)
    if not math.isfinite(result) or result <= 0.0:
        raise ValueError(f"{name} must be a finite value greater than zero.")
    return result


def _nonnegative_float(value: Any, *, name: str) -> float:
    import math

    result = float(value)
    if not math.isfinite(result) or result < 0.0:
        raise ValueError(f"{name} must be a finite value greater than or equal to zero.")
    return result


def _nonnegative_integer(value: Any, *, name: str) -> int:
    import operator

    if isinstance(value, bool):
        raise ValueError(f"{name} must be an integer greater than or equal to zero.")
    try:
        result = operator.index(value)
    except TypeError as exc:
        raise ValueError(
            f"{name} must be an integer greater than or equal to zero."
        ) from exc
    if result < 0:
        raise ValueError(f"{name} must be an integer greater than or equal to zero.")
    return result


def _positive_integer(value: Any, *, name: str) -> int:
    result = _nonnegative_integer(value, name=name)
    if result == 0:
        raise ValueError(f"{name} must be an integer greater than zero.")
    return result


def _default_psf() -> Any:
    import numpy as np

    coordinates = np.arange(-2, 3, dtype=np.float32)
    yy, xx = np.meshgrid(coordinates, coordinates, indexing="ij")
    psf = np.exp(-(xx**2 + yy**2) / 2.0).astype(np.float32)
    return psf / psf.sum()


def _validated_psf(path: Path | None) -> Any:
    import numpy as np

    psf = _default_psf() if path is None else _read_image(path, name="psf_image")
    if np.any(psf < 0.0):
        raise ValueError("psf_image must contain only non-negative values.")
    total = float(psf.sum(dtype=np.float64))
    if not np.isfinite(total) or total <= 0.0:
        raise ValueError("psf_image must have a finite, positive sum.")
    return psf / total


class TotalVariationDenoise(ProcessingTool):
    """Denoise a scalar image with total-variation regularization."""

    row_consumption = RowConsumption.MAPPED
    display_name = "Total Variation Denoise"
    documentation = "Denoise an image with Chambolle total-variation regularization."
    category = Category.RESTORATION
    tags = ["restoration", "denoise", "total variation"]
    environment = GENERAL_ENV

    class Inputs(IOModel):
        input_image: IntensityImage
        weight: Annotated[float, GUIMeta(min=0.001, max=1.0, step=0.01)] = 0.1

    class Outputs(IOModel):
        output_image: OutputImage = Template("{input_image.stem}_tv_denoised.tif")

    def process_row(self, arguments: Arguments, *, context: Any = None) -> Any:
        from skimage.restoration import denoise_tv_chambolle

        image = _read_image(arguments.input_image)
        weight = _positive_float(arguments.weight, name="weight")
        result = denoise_tv_chambolle(image, weight=weight, channel_axis=None)
        output = _write_image(arguments.output_image, result)
        return self.Outputs(output_image=output)


class BilateralDenoise(ProcessingTool):
    """Denoise a scalar image with bilateral filtering."""

    row_consumption = RowConsumption.MAPPED
    display_name = "Bilateral Denoise"
    documentation = "Denoise an image while preserving edges with bilateral filtering."
    category = Category.RESTORATION
    tags = ["restoration", "denoise", "bilateral", "edge preserving"]
    environment = GENERAL_ENV

    class Inputs(IOModel):
        input_image: IntensityImage
        sigma_color: Annotated[float, GUIMeta(min=0.001, max=1.0, step=0.01)] = 0.1
        sigma_spatial: Annotated[float, GUIMeta(min=0.1, max=20.0, step=0.1)] = 1.0

    class Outputs(IOModel):
        output_image: OutputImage = Template("{input_image.stem}_bilateral_denoised.tif")

    def process_row(self, arguments: Arguments, *, context: Any = None) -> Any:
        from skimage.restoration import denoise_bilateral

        image = _read_image(arguments.input_image)
        sigma_color = _positive_float(arguments.sigma_color, name="sigma_color")
        sigma_spatial = _positive_float(arguments.sigma_spatial, name="sigma_spatial")
        result = denoise_bilateral(
            image,
            sigma_color=sigma_color,
            sigma_spatial=sigma_spatial,
            channel_axis=None,
        )
        output = _write_image(arguments.output_image, result)
        return self.Outputs(output_image=output)


class GaussianDenoise(ProcessingTool):
    """Apply Gaussian smoothing to a scalar image."""

    row_consumption = RowConsumption.MAPPED
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
        from scipy.ndimage import gaussian_filter

        image = _read_image(arguments.input_image)
        sigma = _positive_float(arguments.sigma, name="sigma")
        result = gaussian_filter(image, sigma=sigma, mode="nearest")
        output = _write_image(arguments.output_image, result)
        return self.Outputs(output_image=output)


class MedianDenoise(ProcessingTool):
    """Apply median denoising to a scalar image."""

    row_consumption = RowConsumption.MAPPED
    display_name = "Median Denoise"
    documentation = "Median denoising backed by SciPy."
    category = Category.RESTORATION
    tags = ["restoration", "denoise", "median"]
    environment = GENERAL_ENV

    class Inputs(IOModel):
        input_image: IntensityImage
        radius: Annotated[int, GUIMeta(min=0, step=1)] = 1

    class Outputs(IOModel):
        output_image: OutputImage = Template("{input_image.stem}_median.tif")

    def process_row(self, arguments: Arguments, *, context: Any = None) -> Any:
        from scipy.ndimage import median_filter

        image = _read_image(arguments.input_image)
        radius = _nonnegative_integer(arguments.radius, name="radius")
        result = median_filter(image, size=2 * radius + 1, mode="nearest")
        output = _write_image(arguments.output_image, result)
        return self.Outputs(output_image=output)


class BackgroundSubtract(ProcessingTool):
    """Subtract a smoothed background estimate from an image."""

    row_consumption = RowConsumption.MAPPED
    display_name = "Background Subtract"
    documentation = "Subtract a Gaussian-smoothed background estimate."
    category = Category.RESTORATION
    tags = ["restoration", "background", "baseline"]
    environment = GENERAL_ENV

    class Inputs(IOModel):
        input_image: IntensityImage
        sigma: Annotated[float, GUIMeta(min=0.2, max=50.0, step=0.1)] = 10.0
        shift_to_zero: bool = True

    class Outputs(IOModel):
        output_image: OutputImage = Template("{input_image.stem}_background_subtracted.tif")

    def process_row(self, arguments: Arguments, *, context: Any = None) -> Any:
        from scipy.ndimage import gaussian_filter

        image = _read_image(arguments.input_image)
        sigma = _positive_float(arguments.sigma, name="sigma")
        background = gaussian_filter(image, sigma=sigma, mode="nearest")
        result = image - background
        if arguments.shift_to_zero:
            result = result - float(result.min())
        output = _write_image(arguments.output_image, result)
        return self.Outputs(output_image=output)


class UnsharpMask(ProcessingTool):
    """Sharpen an image by adding high-frequency residuals."""

    row_consumption = RowConsumption.MAPPED
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
        from skimage.filters import unsharp_mask

        image = _read_image(arguments.input_image)
        sigma = _positive_float(arguments.sigma, name="sigma")
        amount = _nonnegative_float(arguments.amount, name="amount")
        sharpened = unsharp_mask(
            image,
            radius=sigma,
            amount=amount,
            preserve_range=True,
            channel_axis=None,
        )
        output = _write_image(arguments.output_image, sharpened)
        return self.Outputs(output_image=output)


class RichardsonLucyRestoration(ProcessingTool):
    """Apply Richardson-Lucy deconvolution with a validated PSF."""

    row_consumption = RowConsumption.MAPPED
    display_name = "Richardson-Lucy Restoration"
    documentation = "scikit-image-style Richardson-Lucy deconvolution baseline."
    category = Category.DECONVOLUTION
    tags = ["restoration", "deconvolution", "richardson-lucy"]
    environment = GENERAL_ENV

    class Inputs(IOModel):
        input_image: IntensityImage
        psf_image: Annotated[
            Path | None,
            ImageSpec(semantics={Semantic.INTENSITY}, layouts={Layout.PLANAR}),
            GUIMeta("PSF image", connectable=Connectable.NOT_BY_DEFAULT),
        ] = None
        iterations: Annotated[int, GUIMeta(min=1, step=1)] = 10
        clip: bool = False

    class Outputs(IOModel):
        output_image: OutputImage = Template("{input_image.stem}_richardson_lucy.tif")

    def process_row(self, arguments: Arguments, *, context: Any = None) -> Any:
        import numpy as np
        from skimage.restoration import richardson_lucy

        image = _read_image(arguments.input_image)
        if np.any(image < 0.0):
            raise ValueError("input_image must be non-negative for Richardson-Lucy deconvolution.")
        psf = _validated_psf(arguments.psf_image)
        iterations = _positive_integer(arguments.iterations, name="iterations")
        output = _write_image(
            arguments.output_image,
            richardson_lucy(image, psf, num_iter=iterations, clip=arguments.clip),
        )
        return self.Outputs(output_image=output)
