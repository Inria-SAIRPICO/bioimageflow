"""Cellpose3 - fast cell/nuclei segmentation using Cellpose v3."""

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

cellpose_v3_env = EnvironmentSpec(
    name="cellpose-v3-3-1-1",
    dependencies={
        "python": "3.12",
        "pip": ["cellpose==3.1.1.1", "imageio", "numpy", "packaging", "tifffile"],
    },
)


class Cellpose3(ProcessingTool):
    """Segment cells or nuclei using the faster pre-SAM Cellpose v3 models."""

    display_name = "Cellpose 3"
    documentation = (
        "Segment cells or nuclei using Cellpose v3 pretrained models such as "
        "'cyto3' and 'nuclei'. Produces a labeled mask image."
    )
    category = Category.SEGMENTATION
    tags = ["segmentation", "cellpose", "cellpose3", "deep learning"]
    environment = cellpose_v3_env

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
        diameter: Annotated[float, GUIMeta(
            display_name="Object diameter",
            description="Approximate object diameter in pixels. Set to 0 for Cellpose size estimation.",
            min=0.0, max=500.0, step=0.5, group="general",
        )] = 0.0
        model_type: Annotated[str, GUIMeta(
            display_name="Model",
            description="Cellpose v3 pretrained model name, for example 'cyto3' or 'nuclei'.",
            group="general",
        )] = "cyto3"
        channel: Annotated[int, GUIMeta(
            display_name="Segmentation channel",
            description=(
                "Cellpose channel selector: 0 means grayscale/current image; "
                "1 red, 2 green, 3 blue for RGB-like inputs."
            ),
            min=0, max=3, step=1, group="channels",
        )] = 0
        nuclear_channel: Annotated[int, GUIMeta(
            display_name="Nuclear channel",
            description=(
                "Optional nuclear channel selector for cytoplasm models. "
                "Use 0 when no nuclear channel should be supplied."
            ),
            min=0, max=3, step=1, group="channels",
        )] = 0
        flow_threshold: Annotated[float, GUIMeta(
            display_name="Flow threshold",
            description=(
                "Maximum allowed flow reconstruction error. Increase to keep "
                "more ROIs; decrease to reject more ill-shaped ROIs."
            ),
            min=0.0, max=1.0, step=0.05, group="advanced",
        )] = 0.4
        cellprob_threshold: Annotated[float, GUIMeta(
            display_name="Cell probability threshold",
            description=(
                "Threshold for Cellpose's cell-probability output. Lower values "
                "usually return more ROIs; higher values return fewer ROIs."
            ),
            min=-6.0, max=6.0, step=0.5, group="advanced",
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
                description="Label image where each detected object has a unique integer ID.",
            ),
        ] = Path("{input_image.stem}_cellpose3_mask{ext}")
        cell_count: Annotated[int, GUIMeta(
            display_name="Object count",
            description="Number of non-background labels detected in the mask.",
        )]

    def process_row(self, arguments: Arguments, *, context: Any = None) -> Any:
        from cellpose import models  # type: ignore
        import imageio.v3 as iio
        import numpy as np

        image = iio.imread(str(arguments.input_image))
        diameter = arguments.diameter if arguments.diameter > 0 else None

        print(f"Performing Cellpose v3 segmentation (model={arguments.model_type})...")
        model = models.Cellpose(model_type=arguments.model_type)
        masks, _, _, _ = model.eval(
            image,
            diameter=diameter,
            channels=[arguments.channel, arguments.nuclear_channel],
            flow_threshold=arguments.flow_threshold,
            cellprob_threshold=arguments.cellprob_threshold,
        )

        cell_count = int(masks.max())
        print(f"Cellpose v3: {cell_count} objects detected")

        mask_path = Path(arguments.mask)
        mask_path.parent.mkdir(parents=True, exist_ok=True)
        iio.imwrite(str(mask_path), masks.astype(np.uint32))

        return self.Outputs(mask=mask_path, cell_count=cell_count)
