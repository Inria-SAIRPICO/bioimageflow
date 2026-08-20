"""ConnectedComponents — label connected components in a binary image."""

from pathlib import Path
from typing import Annotated, Any

from bioimageflow_core import (
    Arguments,
    Category,
    Connectable,
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


class ConnectedComponents(ProcessingTool):
    """Label face-connected components in a binary image.

    Converts a binary detection map into a labeled image where each
    connected region receives a unique integer identifier.
    """
    row_consumption = RowConsumption.MAPPED
    display_name = "Connected Components"
    documentation = (
        "Convert a binary image into a labeled image by assigning "
        "a unique identifier to each connected component."
    )
    category = Category.SEGMENTATION
    tags = ["labeling", "segmentation"]
    environment = GENERAL_ENV

    class Inputs(IOModel):
        input_image: Annotated[
            Path,
            ImageSpec(
                semantics={Semantic.BINARY},
                layouts={Layout.PLANAR, Layout.VOLUMETRIC},
            ),
            GUIMeta(
                display_name="Input image",
                description="Binary image (non-zero pixels are foreground) whose connected components are labelled.",
                connectable=Connectable.BY_DEFAULT,
            ),
        ]

    class Outputs(IOModel):
        output_image: Annotated[
            Path,
            ImageSpec(
                semantics={Semantic.LABEL},
                layouts={Layout.PLANAR, Layout.VOLUMETRIC},
                dtypes={"uint32"},
            ),
            GUIMeta(
                display_name="Label image",
                description="Label image where each connected component has a unique integer ID.",
            ),
        ] = Template("{input_image.stem}_labels.tif")
        num_labels: Annotated[int, GUIMeta(
            display_name="Label count",
            description="Number of connected components labelled in the image.",
        )]

    def process_row(self, arguments: Arguments, *, context: Any = None) -> Any:
        import imageio.v3 as iio
        import numpy as np
        from skimage.measure import label

        print("Computing connected components...")
        output_path = Path(arguments.output_image)
        if output_path.suffix.lower() not in {".tif", ".tiff"}:
            raise ValueError(
                "ConnectedComponents output must use a .tif or .tiff extension "
                "to preserve UInt32 labels."
            )
        image = iio.imread(str(arguments.input_image))
        if image.ndim not in {2, 3}:
            raise ValueError(
                "Input image must be a 2D or 3D binary image; "
                f"got shape {image.shape}."
            )
        if not np.isfinite(image).all():
            raise ValueError("Input image must contain only finite values.")
        foreground = image != 0
        labeled_result: Any = label(
            foreground,
            connectivity=1,
            return_num=True,
        )
        labeled_array, num_labels = labeled_result
        num_labels = int(num_labels)
        if num_labels > np.iinfo(np.uint32).max:
            raise OverflowError("Connected-component count exceeds uint32 capacity.")
        labeled_array = labeled_array.astype(np.uint32, copy=False)
        print(f"Connected components: {num_labels} labels")

        output_path.parent.mkdir(parents=True, exist_ok=True)
        iio.imwrite(str(output_path), labeled_array)

        return self.Outputs(output_image=output_path, num_labels=num_labels)
