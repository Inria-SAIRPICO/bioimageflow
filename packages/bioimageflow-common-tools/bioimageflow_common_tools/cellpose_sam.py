"""CellposeSAM — cell/nuclei segmentation using Cellpose."""

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
)

cellpose_env = EnvironmentSpec(
    name="cellpose-sam",
    dependencies={"python": "3.12", "pip": ["cellpose[gui]==3.1.0", "numpy", "imageio"]},
)


class CellposeSAM(ProcessingTool):
    """Segment cells or nuclei using Cellpose.

    Uses the Cellpose deep-learning model for instance segmentation,
    producing a labeled mask where each cell/nucleus has a unique ID.
    """
    display_name = "Cellpose SAM"
    documentation = (
        "Segment cells or nuclei using the Cellpose algorithm. "
        "Produces a labeled mask image."
    )
    category = Category.SEGMENTATION
    tags = ["segmentation", "cellpose", "deep learning"]
    environment = cellpose_env

    class Inputs(IOModel):
        input_image: Annotated[
            Path,
            ImageSpec(
                semantics={Semantic.INTENSITY},
                layouts={Layout.PLANAR, Layout.PLANAR_CHANNEL},
            ),
        ]
        diameter: Annotated[
            float, GUIMeta(connectable=Connectable.NOT_BY_DEFAULT, min=0.0, max=500.0, step=0.5)
        ] = 0.0
        model_type: Annotated[str, GUIMeta(connectable=Connectable.NOT_BY_DEFAULT)] = "cyto3"
        flow_threshold: Annotated[
            float, GUIMeta(connectable=Connectable.NOT_BY_DEFAULT, min=0.0, max=1.0, step=0.05)
        ] = 0.4
        cellprob_threshold: Annotated[
            float, GUIMeta(connectable=Connectable.NOT_BY_DEFAULT, min=-6.0, max=6.0, step=0.5)
        ] = 0.0

    class Outputs(IOModel):
        mask: Annotated[
            Path,
            ImageSpec(
                semantics={Semantic.LABEL},
                layouts={Layout.PLANAR},
            ),
        ] = Path("{input_image.stem}_mask{ext}")
        cell_count: int

    def process_row(self, arguments: Arguments) -> Any:
        from cellpose import models #type: ignore
        import numpy as np
        import imageio.v3 as iio

        img = iio.imread(str(arguments.input_image))

        print(f"Performing Cellpose segmentation (model={arguments.model_type})...")
        model = models.Cellpose(model_type=arguments.model_type)
        diameter = arguments.diameter if arguments.diameter > 0 else None
        masks, _, _, _ = model.eval(
            img,
            diameter=diameter,
            flow_threshold=arguments.flow_threshold,
            cellprob_threshold=arguments.cellprob_threshold,
        )

        cell_count = int(masks.max())
        print(f"Cellpose: {cell_count} cells detected")

        mask_path = Path(arguments.mask)
        mask_path.parent.mkdir(parents=True, exist_ok=True)
        iio.imwrite(str(mask_path), masks.astype(np.uint32))

        return self.Outputs(mask=mask_path, cell_count=cell_count)
