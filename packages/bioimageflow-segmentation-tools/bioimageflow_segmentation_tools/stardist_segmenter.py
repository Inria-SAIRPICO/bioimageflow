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

stardist_env = EnvironmentSpec(
    name="segmentation-stardist",
    dependencies={
        "python": "3.12",
        "pip": [
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
                    "Channel index to segment for channel-first images. Ignored for "
                    "2D grayscale images and for the H&E RGB model."
                ),
                min=0,
                max=512,
                step=1,
                group="channels",
            ),
        ] = 0
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
        ] = Template("{input_image.stem}_stardist_mask{ext}")
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
        import numpy as np

        image = iio.imread(str(arguments.input_image))
        image = self._prepare_image(image, arguments.model_name, arguments.channel)
        normalized = normalize(
            image,
            arguments.normalize_low,
            arguments.normalize_high,
            axis=(0, 1),
        )

        print(f"Performing StarDist segmentation (model={arguments.model_name})...")
        model = self._get_model(arguments.model_name)
        predict_kwargs: dict[str, float] = {}
        if arguments.prob_thresh is not None:
            predict_kwargs["prob_thresh"] = arguments.prob_thresh
        if arguments.nms_thresh is not None:
            predict_kwargs["nms_thresh"] = arguments.nms_thresh
        labels, _ = model.predict_instances(normalized, **predict_kwargs)

        object_count = int(labels.max())
        print(f"StarDist: {object_count} objects detected")

        mask_path = Path(arguments.mask)
        mask_path.parent.mkdir(parents=True, exist_ok=True)
        iio.imwrite(str(mask_path), labels.astype(np.uint32))

        return self.Outputs(mask=mask_path, object_count=object_count)

    @staticmethod
    def _prepare_image(image: Any, model_name: str, channel: int) -> Any:
        if image.ndim == 2:
            return image
        if model_name == "2D_versatile_he":
            if image.ndim == 3 and image.shape[0] in {3, 4}:
                image = image[:3].transpose(1, 2, 0)
            elif image.ndim == 3 and image.shape[-1] == 4:
                image = image[..., :3]
            return image
        if image.ndim == 3:
            if image.shape[-1] in {3, 4} and image.shape[0] > 4:
                return image[..., channel]
            return image[channel]
        raise ValueError(
            f"StarDistSegmenter expects a 2D or 3D channel image, got shape {image.shape}"
        )
