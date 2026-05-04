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
            GUIMeta(
                display_name="Input image",
                description="Fluorescence or brightfield image to segment (2D intensity, optionally multi-channel).",
                connectable=Connectable.BY_DEFAULT,
            ),
        ]
        diameter: Annotated[float, GUIMeta(
            display_name="Cell diameter",
            description="Approximate cell diameter in pixels. Set to 0 for automatic estimation.",
            min=0.0, max=500.0, step=0.5,
        )] = 0.0
        model_type: Annotated[str, GUIMeta(
            display_name="Model",
            description="Cellpose pretrained model name (e.g. 'cyto3', 'nuclei').",
        )] = "cyto3"
        flow_threshold: Annotated[float, GUIMeta(
            display_name="Flow threshold",
            description=(
                "Maximum allowed mean squared error between the flows recomputed "
                "from predicted ROIs and the flows predicted by the network. "
                "Increase this threshold if Cellpose is not returning as many "
                "ROIs as expected; decrease it if Cellpose is returning too "
                "many ill-shaped ROIs."
            ),
            min=0.0, max=1.0, step=0.05,
        )] = 0.4
        cellprob_threshold: Annotated[float, GUIMeta(
            display_name="Cell probability threshold",
            description=(
                "Threshold on the network's cell-probability output (sigmoid "
                "input, typically in the range -6 to +6). Pixels above this "
                "threshold are used to run the flow dynamics and form ROIs. "
                "Decrease the threshold if Cellpose is not returning as many "
                "ROIs as expected; increase it if Cellpose is returning too "
                "many ROIs, particularly from dim areas."
            ),
            min=-6.0, max=6.0, step=0.5,
        )] = 0.0

    class Outputs(IOModel):
        mask: Annotated[
            Path,
            ImageSpec(
                semantics={Semantic.LABEL},
                layouts={Layout.PLANAR},
            ),
            GUIMeta(
                display_name="Segmentation mask",
                description="Label image; each detected cell/nucleus has a unique integer ID.",
            ),
        ] = Path("{input_image.stem}_mask{ext}")
        cell_count: Annotated[int, GUIMeta(
            display_name="Cell count",
            description="Number of cells (non-zero labels) detected in the image.",
        )]

    def process_row(self, arguments: Arguments, *, context: Any = None) -> Any:
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
