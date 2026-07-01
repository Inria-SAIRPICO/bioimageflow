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
            "numpy==2.4.2",
            "tifffile==2026.3.3",
        ],
    }
)


class CellposeSAM(ProcessingTool):
    """Segment cells or nuclei with Cellpose-SAM models."""

    display_name = "Cellpose-SAM"
    documentation = "Segment cells or nuclei using Cellpose-SAM and write a label mask."
    category = Category.SEGMENTATION
    tags = ["segmentation", "cellpose", "sam", "deep learning"]
    environment = cellpose_sam_env

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
        from cellpose import models  # type: ignore
        import imageio.v3 as iio
        import numpy as np

        image = iio.imread(arguments.input_image)
        diameter = arguments.diameter if arguments.diameter > 0 else None
        model_cls = getattr(models, "CellposeModel", None) or getattr(models, "Cellpose")
        model = model_cls(model_type=arguments.model_type)
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
