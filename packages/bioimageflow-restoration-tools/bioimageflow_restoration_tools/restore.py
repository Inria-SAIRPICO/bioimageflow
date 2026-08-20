"""Learned restoration inference and restoration metrics."""

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
    RowConsumption,
    Semantic,
    Template,
)


careamics_env = EnvironmentSpec(
    name="restoration-careamics",
    dependencies={
        "python": "3.12",
        "pip": [
            "careamics==0.3.2",
            "imageio==2.37.3",
            "numpy==2.4.6",
            "tifffile==2026.3.3",
        ],
    }
)


class CAREamicsPredict(ProcessingTool):
    """Run CAREamics prediction from a restoration checkpoint."""

    row_consumption = RowConsumption.MAPPED
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
            Path,
            GUIMeta(
                display_name="Checkpoint",
                description="CAREamics checkpoint containing the model and configuration.",
                connectable=Connectable.NEVER,
            ),
        ]

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
        if image.ndim != 2:
            raise ValueError(f"input_image must be a 2D image; got shape {image.shape}.")
        if image.size == 0 or not np.isfinite(image).all():
            raise ValueError("input_image must be non-empty and contain only finite values.")
        checkpoint = Path(arguments.checkpoint)
        if not checkpoint.is_file():
            raise ValueError(f"checkpoint must be an existing file: {checkpoint}")
        restored = _careamics_predict(image, checkpoint)
        output = Path(arguments.output_image)
        output.parent.mkdir(parents=True, exist_ok=True)
        iio.imwrite(output, np.asarray(restored, dtype=np.float32))
        return self.Outputs(output_image=output, model_source=str(checkpoint))


class RestorationMetrics(ProcessingTool):
    """Compare degraded and restored images against a clean reference."""

    row_consumption = RowConsumption.MAPPED
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
        data_range: Annotated[
            float | None,
            GUIMeta(
                display_name="Data range",
                description="Positive intensity range used to calculate PSNR. Required for constant references.",
                min=0.001,
            ),
        ] = None

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
        from skimage.metrics import mean_squared_error, peak_signal_noise_ratio

        clean = iio.imread(arguments.clean_image).astype(np.float64)
        degraded = iio.imread(arguments.degraded_image).astype(np.float64)
        restored = iio.imread(arguments.restored_image).astype(np.float64)
        if any(image.ndim != 2 for image in (clean, degraded, restored)):
            raise ValueError(
                "clean_image, degraded_image, and restored_image must be 2D images."
            )
        if clean.shape != degraded.shape or clean.shape != restored.shape:
            raise ValueError("clean_image, degraded_image, and restored_image must match.")
        if clean.size == 0:
            raise ValueError("Metric input images must not be empty.")
        if not all(np.isfinite(image).all() for image in (clean, degraded, restored)):
            raise ValueError("Metric input images must contain only finite values.")

        if arguments.data_range is None:
            data_range = float(clean.max() - clean.min())
            if data_range <= 0.0:
                raise ValueError(
                    "data_range is required when clean_image has a constant value."
                )
        else:
            data_range = float(arguments.data_range)
            if not np.isfinite(data_range) or data_range <= 0.0:
                raise ValueError("data_range must be a finite value greater than zero.")

        mse_degraded = float(mean_squared_error(clean, degraded))
        mse_restored = float(mean_squared_error(clean, restored))
        with np.errstate(divide="ignore"):
            degraded_psnr = float(
                peak_signal_noise_ratio(clean, degraded, data_range=data_range)
            )
            restored_psnr = float(
                peak_signal_noise_ratio(clean, restored, data_range=data_range)
            )
        return self.Outputs(
            clean_image=str(arguments.clean_image),
            degraded_image=str(arguments.degraded_image),
            restored_image=str(arguments.restored_image),
            mse_degraded=mse_degraded,
            mse_restored=mse_restored,
            degraded_psnr=degraded_psnr,
            restored_psnr=restored_psnr,
            residual_noise_degraded=float(np.std(degraded - clean)),
            residual_noise_restored=float(np.std(restored - clean)),
        )


def _careamics_predict(image: Any, checkpoint: Path) -> Any:
    import numpy as np

    careamics = __import__("careamics")
    model = careamics.CAREamist(
        checkpoint_path=checkpoint,
        enable_progress_bar=False,
    )
    result = model.predict(pred_data=image, axes="YX", data_type="array")
    if not isinstance(result, tuple) or len(result) != 2:
        raise ValueError("CAREamist.predict must return (predictions, sources).")
    predictions, _sources = result
    if not isinstance(predictions, (list, tuple)) or len(predictions) != 1:
        raise ValueError("CAREamics prediction must return exactly one image.")
    restored = np.asarray(predictions[0], dtype=np.float32)
    if restored.shape != image.shape:
        raise ValueError(
            "CAREamics prediction shape must match input_image: "
            f"expected {image.shape}, got {restored.shape}."
        )
    if not np.isfinite(restored).all():
        raise ValueError("CAREamics prediction must contain only finite values.")
    return restored
