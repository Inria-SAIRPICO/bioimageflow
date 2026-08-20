"""Cellpose-SAM segmentation wrapper."""

from pathlib import Path
from typing import Annotated, Any

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

    def _get_model(self, model_type: str) -> Any:
        """Return the model for *model_type*, replacing a different cached model."""
        if self._cached_model is None or self._model_cache_key != model_type:
            from cellpose import models  # type: ignore

            self.clear_model_cache()
            model_cls = getattr(models, "CellposeModel", None) or getattr(
                models, "Cellpose"
            )
            model = model_cls(model_type=model_type)
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
                description="2D intensity image to segment.",
                connectable=Connectable.BY_DEFAULT,
            ),
        ]
        model_type: Annotated[
            str,
            GUIMeta(display_name="Model", description="Cellpose-SAM model name."),
        ] = "cpsam"
        diameter: Annotated[float, GUIMeta(min=0.0, max=500.0, step=0.5)] = 0.0
        flow_threshold: Annotated[float, GUIMeta(min=0.0, max=1.0, step=0.05)] = 0.4
        cellprob_threshold: Annotated[float, GUIMeta(min=-6.0, max=6.0, step=0.5)] = 0.0

    class Outputs(IOModel):
        mask: Annotated[
            Path,
            ImageSpec(semantics={Semantic.LABEL}, layouts={Layout.PLANAR}),
            GUIMeta(display_name="Segmentation mask"),
        ] = Template("{input_image.stem}_cellpose_sam_mask{ext}")
        cell_count: Annotated[int, GUIMeta(display_name="Object count")]

    def process_row(self, arguments: Arguments, *, context: Any = None) -> Any:
        import imageio.v3 as iio
        import numpy as np

        image = iio.imread(arguments.input_image)
        diameter = arguments.diameter if arguments.diameter > 0 else None
        model = self._get_model(arguments.model_type)
        result = model.eval(
            image,
            diameter=diameter,
            flow_threshold=arguments.flow_threshold,
            cellprob_threshold=arguments.cellprob_threshold,
        )
        masks = result[0] if isinstance(result, tuple) else result

        mask_path = Path(arguments.mask)
        mask_path.parent.mkdir(parents=True, exist_ok=True)
        iio.imwrite(mask_path, np.asarray(masks, dtype=np.uint32))
        return self.Outputs(mask=mask_path, cell_count=int(np.asarray(masks).max()))
