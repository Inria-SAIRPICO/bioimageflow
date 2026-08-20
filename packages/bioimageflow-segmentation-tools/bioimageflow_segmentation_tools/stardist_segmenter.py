"""StarDistSegmenter - nuclei segmentation using StarDist 2D."""

from pathlib import Path
from typing import Annotated, Any, Literal

from bioimageflow_core import (
    Arguments,
    Category,
    Connectable,
    EnvironmentSpec,
    GUIMeta,
    ImageSpec,
    IOModel,
    Layout,
    ProcessingTool,
    RowConsumption,
    Semantic,
    Template,
)

from ._arrays import (
    finite_float,
    integer_parameter,
    object_count,
    validate_image,
    validate_labels,
    write_labels,
)

stardist_env = EnvironmentSpec(
    name="segmentation-stardist",
    dependencies={
        "python": "3.12",
        "pip": [
            "csbdeep==0.8.2",
            "tensorflow==2.21.0",
            "stardist==0.9.2",
            "imageio==2.37.3",
            "numpy==2.4.6",
            "tifffile==2026.6.1",
        ],
    }
)


class StarDistSegmenter(ProcessingTool):
    """Segment nuclei/objects in 2D images using pretrained StarDist models."""

    row_consumption = RowConsumption.MAPPED
    display_name = "StarDist 2D"
    documentation = (
        "Segment star-convex nuclei or cells in 2D images using StarDist "
        "pretrained models. Produces a labeled mask image."
    )
    category = Category.SEGMENTATION
    tags = ["segmentation", "stardist", "nuclei", "deep learning"]
    environment = stardist_env

    def __init__(self) -> None:
        super().__init__()
        self._model_cache_key: str | None = None
        self._cached_model: Any | None = None

    def clear_model_cache(self) -> None:
        """Release the cached StarDist model held by this tool instance."""
        self._cached_model = None
        self._model_cache_key = None

    def _get_model(self, model_name: str) -> Any:
        """Return the model for *model_name*, replacing a different cached model."""
        if self._cached_model is None or self._model_cache_key != model_name:
            from stardist.models import StarDist2D  # type: ignore

            self.clear_model_cache()
            model = StarDist2D.from_pretrained(model_name)
            self._cached_model = model
            self._model_cache_key = model_name
        return self._cached_model

    class Inputs(IOModel):
        input_image: Annotated[
            Path,
            ImageSpec(
                semantics={Semantic.INTENSITY},
                layouts={Layout.PLANAR, Layout.PLANAR_CHANNEL},
            ),
            GUIMeta(
                display_name="Input image",
                description="2D fluorescence or RGB/H&E image to segment.",
                connectable=Connectable.BY_DEFAULT,
            ),
        ]
        model_name: Annotated[
            Literal["2D_versatile_fluo", "2D_paper_dsb2018", "2D_versatile_he"],
            GUIMeta(
                display_name="Model",
                description=(
                    "Pretrained StarDist model. Use fluorescence models for "
                    "single-channel nuclear images and H&E for RGB histology."
                ),
                group="general",
            ),
        ] = "2D_versatile_fluo"
        channel: Annotated[
            int,
            GUIMeta(
                display_name="Channel",
                description=(
                    "Zero-based channel index for fluorescence images. Must be zero "
                    "for 2D grayscale images and is ignored for the H&E model."
                ),
                min=0,
                max=512,
                step=1,
                group="channels",
            ),
        ] = 0
        channel_axis: Annotated[
            Literal["first", "last"],
            GUIMeta(
                display_name="Channel axis",
                description="Location of the channel axis in three-dimensional input images.",
                group="channels",
            ),
        ] = "last"
        prob_thresh: Annotated[
            float | None,
            GUIMeta(
                display_name="Probability threshold",
                description="Optional object probability threshold. Leave empty for the model default.",
                min=0.0,
                max=1.0,
                step=0.05,
                group="advanced",
            ),
        ] = None
        nms_thresh: Annotated[
            float | None,
            GUIMeta(
                display_name="NMS threshold",
                description="Optional non-maximum suppression threshold. Leave empty for the model default.",
                min=0.0,
                max=1.0,
                step=0.05,
                group="advanced",
            ),
        ] = None
        normalize_low: Annotated[
            float,
            GUIMeta(
                display_name="Normalize low percentile",
                description="Lower percentile used by csbdeep normalization.",
                min=0.0,
                max=100.0,
                step=0.5,
                group="advanced",
            ),
        ] = 1.0
        normalize_high: Annotated[
            float,
            GUIMeta(
                display_name="Normalize high percentile",
                description="Upper percentile used by csbdeep normalization.",
                min=0.0,
                max=100.0,
                step=0.5,
                group="advanced",
            ),
        ] = 99.8

    class Outputs(IOModel):
        mask: Annotated[
            Path,
            ImageSpec(
                semantics={Semantic.LABEL},
                layouts={Layout.PLANAR},
            ),
            GUIMeta(
                display_name="Segmentation mask",
                description="Label image where each detected object has a unique integer ID.",
            ),
        ] = Template("{input_image.stem}_stardist_mask.tif")
        object_count: Annotated[
            int,
            GUIMeta(
                display_name="Object count",
                description="Number of non-background labels detected in the mask.",
            ),
        ]

    def process_row(self, arguments: Arguments, *, context: Any = None) -> Any:
        from csbdeep.utils import normalize  # type: ignore
        import imageio.v3 as iio

        source = validate_image(
            iio.imread(str(arguments.input_image)),
            name="input_image",
            dimensions=(2, 3),
        )
        channel = integer_parameter(arguments.channel, name="channel")
        model_name = str(arguments.model_name)
        if model_name not in {
            "2D_versatile_fluo",
            "2D_paper_dsb2018",
            "2D_versatile_he",
        }:
            raise ValueError(f"Unsupported StarDist model_name: {model_name!r}.")
        channel_axis = str(arguments.channel_axis)
        if channel_axis not in {"first", "last"}:
            raise ValueError("channel_axis must be 'first' or 'last'.")
        image = self._prepare_image(source, model_name, channel, channel_axis)
        normalize_low = finite_float(arguments.normalize_low, name="normalize_low")
        normalize_high = finite_float(arguments.normalize_high, name="normalize_high")
        if not 0.0 <= normalize_low < normalize_high <= 100.0:
            raise ValueError(
                "normalize_low and normalize_high must satisfy "
                "0 <= normalize_low < normalize_high <= 100."
            )
        normalized = normalize(
            image,
            normalize_low,
            normalize_high,
            axis=(0, 1),
        )

        print(f"Performing StarDist segmentation (model={model_name})...")
        model = self._get_model(model_name)
        predict_kwargs: dict[str, float] = {}
        if arguments.prob_thresh is not None:
            prob_thresh = finite_float(arguments.prob_thresh, name="prob_thresh")
            if not 0.0 <= prob_thresh <= 1.0:
                raise ValueError("prob_thresh must be between 0 and 1.")
            predict_kwargs["prob_thresh"] = prob_thresh
        if arguments.nms_thresh is not None:
            nms_thresh = finite_float(arguments.nms_thresh, name="nms_thresh")
            if not 0.0 <= nms_thresh <= 1.0:
                raise ValueError("nms_thresh must be between 0 and 1.")
            predict_kwargs["nms_thresh"] = nms_thresh
        labels, _ = model.predict_instances(normalized, **predict_kwargs)
        labels = validate_labels(
            labels,
            name="StarDist mask",
            dimensions=(2,),
            expected_shape=image.shape[:2],
        )

        count = object_count(labels)
        print(f"StarDist: {count} objects detected")

        mask_path = Path(arguments.mask)
        write_labels(mask_path, labels)

        return self.Outputs(mask=mask_path, object_count=count)

    @staticmethod
    def _prepare_image(
        image: Any,
        model_name: str,
        channel: int,
        channel_axis: str,
    ) -> Any:
        import numpy as np

        if image.ndim == 2:
            if model_name == "2D_versatile_he":
                raise ValueError("The H&E StarDist model requires an RGB image.")
            if channel != 0:
                raise ValueError("A 2D grayscale image requires channel=0.")
            return image
        if model_name == "2D_versatile_he":
            axis = 0 if channel_axis == "first" else -1
            channel_count = image.shape[axis]
            if channel_count not in {3, 4}:
                raise ValueError(
                    "The H&E StarDist model requires three RGB channels or four RGBA "
                    f"channels on the declared channel axis; got shape {image.shape}."
                )
            channel_last = np.moveaxis(image, axis, -1)
            return channel_last[..., :3]

        axis = 0 if channel_axis == "first" else image.ndim - 1
        channel_count = image.shape[axis]
        if channel >= channel_count:
            raise ValueError(
                f"channel={channel} is out of range for {channel_count} channels."
            )
        return np.take(image, channel, axis=axis)
