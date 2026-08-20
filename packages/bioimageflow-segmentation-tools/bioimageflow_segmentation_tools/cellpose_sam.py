"""Cellpose-SAM segmentation wrapper."""

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

from ._arrays import finite_float, object_count, validate_image, validate_labels, write_labels


cellpose_sam_env = EnvironmentSpec(
    name="segmentation-cellpose-sam",
    dependencies={
        "python": "3.12",
        "pip": [
            "cellpose==4.2.1.1",
            "imageio==2.37.3",
            "numpy==2.5.0",
            "tifffile==2026.6.1",
        ],
    }
)


class CellposeSAM(ProcessingTool):
    """Segment cells or nuclei with Cellpose-SAM models."""

    row_consumption = RowConsumption.MAPPED
    display_name = "Cellpose-SAM"
    documentation = "Segment cells or nuclei using Cellpose-SAM and write a label mask."
    category = Category.SEGMENTATION
    tags = ["segmentation", "cellpose", "sam", "deep learning"]
    environment = cellpose_sam_env

    def __init__(self) -> None:
        super().__init__()
        self._model_cache_key: str | None = None
        self._cached_model: Any | None = None

    def clear_model_cache(self) -> None:
        """Release the cached Cellpose-SAM model held by this tool instance."""
        self._cached_model = None
        self._model_cache_key = None

    def _get_model(self, pretrained_model: str) -> Any:
        """Return the selected model, replacing a different cached model."""
        if self._cached_model is None or self._model_cache_key != pretrained_model:
            from cellpose import models  # type: ignore

            self.clear_model_cache()
            model = models.CellposeModel(pretrained_model=pretrained_model)
            self._cached_model = model
            self._model_cache_key = pretrained_model
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
                description="2D intensity image to segment.",
                connectable=Connectable.BY_DEFAULT,
            ),
        ]
        pretrained_model: Annotated[
            str,
            GUIMeta(
                display_name="Pretrained model",
                description="Cellpose-SAM built-in model name or checkpoint path.",
            ),
        ] = "cpsam_v2"
        diameter: Annotated[float, GUIMeta(min=0.0, max=500.0, step=0.5)] = 0.0
        channel_axis: Annotated[
            Literal["first", "last"],
            GUIMeta(
                display_name="Channel axis",
                description="Location of the channel axis in three-dimensional input images.",
            ),
        ] = "last"
        flow_threshold: Annotated[float, GUIMeta(min=0.0, max=1.0, step=0.05)] = 0.4
        cellprob_threshold: Annotated[float, GUIMeta(min=-6.0, max=6.0, step=0.5)] = 0.0

    class Outputs(IOModel):
        mask: Annotated[
            Path,
            ImageSpec(semantics={Semantic.LABEL}, layouts={Layout.PLANAR}),
            GUIMeta(display_name="Segmentation mask"),
        ] = Template("{input_image.stem}_cellpose_sam_mask.tif")
        cell_count: Annotated[int, GUIMeta(display_name="Object count")]

    def process_row(self, arguments: Arguments, *, context: Any = None) -> Any:
        import imageio.v3 as iio
        import numpy as np

        image = validate_image(
            iio.imread(str(arguments.input_image)),
            name="input_image",
            dimensions=(2, 3),
        )
        pretrained_model = str(arguments.pretrained_model).strip()
        if not pretrained_model:
            raise ValueError("pretrained_model must not be empty.")
        diameter_value = finite_float(arguments.diameter, name="diameter")
        if diameter_value < 0:
            raise ValueError("diameter must be greater than or equal to zero.")
        diameter = diameter_value if diameter_value > 0 else None
        flow_threshold = finite_float(arguments.flow_threshold, name="flow_threshold")
        cellprob_threshold = finite_float(
            arguments.cellprob_threshold,
            name="cellprob_threshold",
        )
        if not 0.0 <= flow_threshold <= 1.0:
            raise ValueError("flow_threshold must be between 0 and 1.")
        if not -6.0 <= cellprob_threshold <= 6.0:
            raise ValueError("cellprob_threshold must be between -6 and 6.")
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
                    "Three-dimensional Cellpose-SAM input must have one to four channels "
                    f"on the declared channel axis; got shape {image.shape}."
                )
            expected_shape = tuple(
                size for axis, size in enumerate(image.shape) if axis != channel_axis
            )
        model = self._get_model(pretrained_model)
        result = model.eval(
            image,
            diameter=diameter,
            channel_axis=channel_axis,
            flow_threshold=flow_threshold,
            cellprob_threshold=cellprob_threshold,
        )
        masks = result[0] if isinstance(result, tuple) else result
        masks = validate_labels(
            masks,
            name="Cellpose-SAM mask",
            dimensions=(2,),
            expected_shape=expected_shape,
        )

        mask_path = Path(arguments.mask)
        write_labels(mask_path, np.asarray(masks, dtype=np.uint32))
        return self.Outputs(mask=mask_path, cell_count=object_count(masks))
