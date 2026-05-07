"""Restoration baseline backed by scikit-image when available."""

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


def restore_array(image: "Any", method: str = "tv_chambolle", weight: float = 0.1) -> "Any":
    """Restore an image with scikit-image if installed, otherwise a light fallback."""
    import numpy as np

    image = image.astype(np.float32, copy=False)
    method = method.lower()
    try:
        from skimage.restoration import denoise_tv_chambolle, denoise_bilateral
    except ImportError:
        if method in {"tv_chambolle", "gaussian"}:
            return gaussian_blur(image, max(float(weight) * 4.0, 0.4))
        if method == "unsharp":
            low = gaussian_blur(image, 1.0)
            return np.clip(image + float(weight) * (image - low), 0.0, 1.0)
        raise ValueError(f"Unsupported restoration method: {method}")

    if method == "tv_chambolle":
        return denoise_tv_chambolle(image, weight=float(weight)).astype(np.float32)
    if method == "bilateral":
        return denoise_bilateral(image, sigma_color=float(weight), channel_axis=None).astype(
            np.float32
        )
    if method == "gaussian":
        return gaussian_blur(image, max(float(weight) * 4.0, 0.4))
    if method == "unsharp":
        low = gaussian_blur(image, 1.0)
        return np.clip(image + float(weight) * (image - low), 0.0, 1.0)
    raise ValueError(f"Unsupported restoration method: {method}")


class RestoreImage(ProcessingTool):
    """Apply a scikit-image restoration baseline to a scalar image."""

    display_name = "Restore Image"
    documentation = (
        "Restore a noisy or blurred image with a scikit-image baseline. "
        "When scikit-image is unavailable, a small NumPy fallback keeps default "
        "tests and examples runnable."
    )
    category = Category.RESTORATION
    tags = ["restoration", "denoise", "scikit-image"]
    environment = GENERAL_ENV

    class Inputs(IOModel):
        input_image: Annotated[
            Path,
            ImageSpec(semantics={Semantic.INTENSITY}, layouts={Layout.PLANAR}),
            GUIMeta(
                display_name="Input image",
                description="2D scalar image to restore.",
                connectable=Connectable.BY_DEFAULT,
            ),
        ]
        method: str = "tv_chambolle"
        weight: Annotated[float, GUIMeta(min=0.0, max=1.0, step=0.01)] = 0.1

    class Outputs(IOModel):
        output_image: Annotated[
            Path,
            ImageSpec(semantics={Semantic.INTENSITY}, layouts={Layout.PLANAR}),
            GUIMeta(display_name="Restored image"),
        ] = Template("{input_image.stem}_restored.tif")

    def process_row(self, arguments: Arguments, *, context: Any = None) -> Any:
        import imageio.v3 as iio
        import numpy as np

        image = iio.imread(arguments.input_image).astype(np.float32)
        restored = restore_array(image, method=arguments.method, weight=arguments.weight)
        output = Path(arguments.output_image)
        output.parent.mkdir(parents=True, exist_ok=True)
        iio.imwrite(output, restored.astype(np.float32))
        return self.Outputs(output_image=output)
