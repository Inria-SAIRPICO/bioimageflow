"""Cellpose3 - fast cell/nuclei segmentation using Cellpose v3."""

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

cellpose_v3_env = EnvironmentSpec(
    name="segmentation-cellpose-v3",
    dependencies={
        "python": "3.12",
        "pip": [
            "cellpose==3.1.1.1",
            "imageio==2.37.3",
            "packaging==26.2",
            "tifffile==2026.3.3",
        ],
    },
)


class Cellpose3(ProcessingTool):
    """Segment cells or nuclei using the faster pre-SAM Cellpose v3 models."""

    row_consumption = RowConsumption.MAPPED
    display_name = "Cellpose 3"
    documentation = (
        "Segment cells or nuclei using Cellpose v3 pretrained models such as "
        "'cyto3' and 'nuclei'. Produces a labeled mask image."
    )
    category = Category.SEGMENTATION
    tags = ["segmentation", "cellpose", "cellpose3", "deep learning"]
    environment = cellpose_v3_env

    def __init__(self) -> None:
        super().__init__()
        self._model_cache_key: str | None = None
        self._cached_model: Any | None = None

    def clear_model_cache(self) -> None:
        """Release the cached Cellpose model held by this tool instance."""
        self._cached_model = None
        self._model_cache_key = None

    def _get_model(self, model_type: str) -> Any:
        """Return the model for *model_type*, replacing a different cached model."""
        if self._cached_model is None or self._model_cache_key != model_type:
            from cellpose import models  # type: ignore

            self.clear_model_cache()
            model = models.Cellpose(model_type=model_type)
            self._cached_model = model
            self._model_cache_key = model_type
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
                description="2D intensity image to segment, optionally with channels.",
                connectable=Connectable.BY_DEFAULT,
            ),
        ]
        diameter: Annotated[
            float,
            GUIMeta(
                display_name="Object diameter",
                description=(
                    "Approximate object diameter in pixels. Set to 0 for "
                    "Cellpose size estimation."
                ),
                min=0.0,
                max=500.0,
                step=0.5,
                group="general",
            ),
        ] = 0.0
        model_type: Annotated[
            str,
            GUIMeta(
                display_name="Model",
                description="Cellpose v3 pretrained model name, for example 'cyto3' or 'nuclei'.",
                group="general",
            ),
        ] = "cyto3"
        channel: Annotated[
            int,
            GUIMeta(
                display_name="Segmentation channel",
                description=(
                    "Cellpose channel selector: 0 means grayscale/current image; "
                    "1 red, 2 green, 3 blue for RGB-like inputs."
                ),
                min=0,
                max=3,
                step=1,
                group="channels",
            ),
        ] = 0
        nuclear_channel: Annotated[
            int,
            GUIMeta(
                display_name="Nuclear channel",
                description=(
                    "Optional nuclear channel selector for cytoplasm models. "
                    "Use 0 when no nuclear channel should be supplied."
                ),
                min=0,
                max=3,
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
        flow_threshold: Annotated[
            float,
            GUIMeta(
                display_name="Flow threshold",
                description=(
                    "Maximum allowed flow reconstruction error. Increase to keep "
                    "more ROIs; decrease to reject more ill-shaped ROIs."
                ),
                min=0.0,
                max=1.0,
                step=0.05,
                group="advanced",
            ),
        ] = 0.4
        cellprob_threshold: Annotated[
            float,
            GUIMeta(
                display_name="Cell probability threshold",
                description=(
                    "Threshold for Cellpose's cell-probability output. Lower values "
                    "usually return more ROIs; higher values return fewer ROIs."
                ),
                min=-6.0,
                max=6.0,
                step=0.5,
                group="advanced",
            ),
        ] = 0.0

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
        ] = Template("{input_image.stem}_cellpose3_mask{ext}")
        cell_count: Annotated[
            int,
            GUIMeta(
                display_name="Object count",
                description="Number of non-background labels detected in the mask.",
            ),
        ]

    def process_row(self, arguments: Arguments, *, context: Any = None) -> Any:
        import imageio.v3 as iio

        image = validate_image(
            iio.imread(str(arguments.input_image)),
            name="input_image",
            dimensions=(2, 3),
        )
        diameter_value = finite_float(arguments.diameter, name="diameter")
        if diameter_value < 0:
            raise ValueError("diameter must be greater than or equal to zero.")
        diameter = diameter_value if diameter_value > 0 else None
        model_type = str(arguments.model_type).strip()
        if not model_type:
            raise ValueError("model_type must not be empty.")
        channel = integer_parameter(arguments.channel, name="channel")
        nuclear_channel = integer_parameter(
            arguments.nuclear_channel,
            name="nuclear_channel",
        )
        if channel > 3 or nuclear_channel > 3:
            raise ValueError("channel and nuclear_channel must be between 0 and 3.")
        if image.ndim == 2 and (channel != 0 or nuclear_channel != 0):
            raise ValueError("2D grayscale input requires channel=0 and nuclear_channel=0.")
        channel_axis_name = str(arguments.channel_axis)
        if channel_axis_name not in {"first", "last"}:
            raise ValueError("channel_axis must be 'first' or 'last'.")
        channel_axis = None
        expected_shape = image.shape
        if image.ndim == 3:
            channel_axis = 0 if channel_axis_name == "first" else 2
            channel_count = image.shape[channel_axis]
            if channel_count < 1 or channel_count > 4:
                raise ValueError(
                    "Three-dimensional Cellpose input must have one to four channels "
                    f"on the declared channel axis; got shape {image.shape}."
                )
            expected_shape = tuple(
                size for axis, size in enumerate(image.shape) if axis != channel_axis
            )
        flow_threshold = finite_float(arguments.flow_threshold, name="flow_threshold")
        cellprob_threshold = finite_float(
            arguments.cellprob_threshold,
            name="cellprob_threshold",
        )
        if not 0.0 <= flow_threshold <= 1.0:
            raise ValueError("flow_threshold must be between 0 and 1.")
        if not -6.0 <= cellprob_threshold <= 6.0:
            raise ValueError("cellprob_threshold must be between -6 and 6.")

        print(f"Performing Cellpose v3 segmentation (model={model_type})...")
        model = self._get_model(model_type)
        masks, _, _, _ = model.eval(
            image,
            diameter=diameter,
            channels=[channel, nuclear_channel],
            channel_axis=channel_axis,
            flow_threshold=flow_threshold,
            cellprob_threshold=cellprob_threshold,
        )
        masks = validate_labels(
            masks,
            name="Cellpose 3 mask",
            dimensions=(2,),
            expected_shape=expected_shape,
        )

        cell_count = object_count(masks)
        print(f"Cellpose v3: {cell_count} objects detected")

        mask_path = Path(arguments.mask)
        write_labels(mask_path, masks)

        return self.Outputs(mask=mask_path, cell_count=cell_count)
