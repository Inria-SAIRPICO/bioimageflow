"""Restoration inference, simple baselines, and metrics."""

from pathlib import Path
from typing import Annotated, Any

from bioimageflow_core import (
    Arguments,
    Category,
    Connectable,
    EnvironmentSpec,
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
    """Restore an image with a simple image-processing baseline."""
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
    """Apply a simple image-processing restoration baseline to a scalar image."""

    display_name = "Restore Image"
    documentation = (
        "Restore a noisy or blurred image with a simple image-processing baseline."
    )
    category = Category.RESTORATION
    tags = ["restoration", "denoise", "baseline"]
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


careamics_env = EnvironmentSpec(
    name="restoration-careamics",
    dependencies={
        "python": "3.12",
        "pip": ["careamics", "imageio", "numpy", "tifffile"],
    },
)


class CAREamicsPredict(ProcessingTool):
    """Run CAREamics prediction from a restoration checkpoint."""

    display_name = "CAREamics Predict"
    documentation = (
        "Run CAREamics restoration inference from a checkpoint."
    )
    category = Category.RESTORATION
    tags = ["restoration", "careamics", "noise2void", "deep learning"]
    environment = careamics_env

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
        checkpoint: Annotated[
            Path | None,
            GUIMeta(
                display_name="Checkpoint",
                description="Optional CAREamics checkpoint path.",
                connectable=Connectable.NEVER,
            ),
        ] = None
    class Outputs(IOModel):
        output_image: Annotated[
            Path,
            ImageSpec(semantics={Semantic.INTENSITY}, layouts={Layout.PLANAR}),
            GUIMeta(display_name="Restored image"),
        ] = Template("{input_image.stem}_careamics_restored.tif")
        model_source: Annotated[str, GUIMeta(display_name="Model source")]

    def process_row(self, arguments: Arguments, *, context: Any = None) -> Any:
        import imageio.v3 as iio
        import numpy as np

        image = iio.imread(arguments.input_image).astype(np.float32)
        restored = _careamics_predict(image, arguments.checkpoint)
        output = Path(arguments.output_image)
        output.parent.mkdir(parents=True, exist_ok=True)
        iio.imwrite(output, np.asarray(restored, dtype=np.float32))
        model_source = str(arguments.checkpoint) if arguments.checkpoint else "careamics-default"
        return self.Outputs(output_image=output, model_source=model_source)


class RestorationMetrics(ProcessingTool):
    """Compare degraded and restored images against a clean reference."""

    display_name = "Restoration Metrics"
    documentation = "Compute MSE, PSNR, and residual noise estimates for restoration evaluation."
    category = Category.MEASUREMENT
    tags = ["restoration", "metrics", "psnr", "noise"]
    environment = GENERAL_ENV

    class Inputs(IOModel):
        clean_image: Annotated[
            Path,
            ImageSpec(semantics={Semantic.INTENSITY}, layouts={Layout.PLANAR}),
            GUIMeta(display_name="Clean reference", connectable=Connectable.BY_DEFAULT),
        ]
        degraded_image: Annotated[
            Path,
            ImageSpec(semantics={Semantic.INTENSITY}, layouts={Layout.PLANAR}),
            GUIMeta(display_name="Degraded image", connectable=Connectable.BY_DEFAULT),
        ]
        restored_image: Annotated[
            Path,
            ImageSpec(semantics={Semantic.INTENSITY}, layouts={Layout.PLANAR}),
            GUIMeta(display_name="Restored image", connectable=Connectable.BY_DEFAULT),
        ]

    class Outputs(IOModel):
        clean_image: Annotated[str, GUIMeta(display_name="Clean image")]
        degraded_image: Annotated[str, GUIMeta(display_name="Degraded image")]
        restored_image: Annotated[str, GUIMeta(display_name="Restored image")]
        mse_degraded: Annotated[float, GUIMeta(display_name="MSE degraded")]
        mse_restored: Annotated[float, GUIMeta(display_name="MSE restored")]
        degraded_psnr: Annotated[float, GUIMeta(display_name="Degraded PSNR")]
        restored_psnr: Annotated[float, GUIMeta(display_name="Restored PSNR")]
        residual_noise_degraded: Annotated[float, GUIMeta(display_name="Degraded residual noise")]
        residual_noise_restored: Annotated[float, GUIMeta(display_name="Restored residual noise")]

    def process_row(self, arguments: Arguments, *, context: Any = None) -> Any:
        import imageio.v3 as iio
        import numpy as np

        clean = iio.imread(arguments.clean_image).astype(np.float32)
        degraded = iio.imread(arguments.degraded_image).astype(np.float32)
        restored = iio.imread(arguments.restored_image).astype(np.float32)
        if clean.shape != degraded.shape or clean.shape != restored.shape:
            raise ValueError("clean_image, degraded_image, and restored_image must match.")

        mse_degraded = float(np.mean((degraded - clean) ** 2))
        mse_restored = float(np.mean((restored - clean) ** 2))
        return self.Outputs(
            clean_image=str(arguments.clean_image),
            degraded_image=str(arguments.degraded_image),
            restored_image=str(arguments.restored_image),
            mse_degraded=mse_degraded,
            mse_restored=mse_restored,
            degraded_psnr=_psnr(mse_degraded, clean),
            restored_psnr=_psnr(mse_restored, clean),
            residual_noise_degraded=float(np.std(degraded - clean)),
            residual_noise_restored=float(np.std(restored - clean)),
        )


def _careamics_predict(image: Any, checkpoint: Path | None) -> Any:
    careamics = __import__("careamics")
    if hasattr(careamics, "predict"):
        return careamics.predict(image, checkpoint=checkpoint)
    model = careamics.CAREamist(source=checkpoint)
    return model.predict(image)


def _psnr(mse: float, clean: Any) -> float:
    import math
    import numpy as np

    if mse <= 0.0:
        return float("inf")
    data_range = float(np.max(clean) - np.min(clean)) or 1.0
    return float(20.0 * math.log10(data_range) - 10.0 * math.log10(mse))
