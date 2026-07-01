"""ConnectedComponents — label connected components in a binary image."""

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

simpleitk_env = EnvironmentSpec(
    name="simpleitk",
    dependencies={
        "python": "3.12",
        "pip": ["SimpleITK==2.5.5", "numpy==2.4.2", "tifffile==2026.3.3"],
    },
)


class ConnectedComponents(ProcessingTool):
    """Label connected components in a binary image using SimpleITK.

    Converts a binary detection map into a labeled image where each
    connected region receives a unique integer identifier.
    Uses SimpleITK for input I/O and processing, and tifffile for UInt32 TIFF
    output because SimpleITK's TIFF writer does not support UInt32.
    """
    display_name = "Connected Components"
    documentation = (
        "Convert a binary image into a labeled image by assigning "
        "a unique identifier to each connected component."
    )
    category = Category.SEGMENTATION
    tags = ["labeling", "segmentation"]
    environment = simpleitk_env

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
        ] = Template("{input_image.stem}_labels{ext}")
        num_labels: Annotated[int, GUIMeta(
            display_name="Label count",
            description="Number of connected components labelled in the image.",
        )]

    def process_row(self, arguments: Arguments, *, context: Any = None) -> Any:
        import numpy as np
        import SimpleITK as sitk    # type: ignore
        import tifffile

        sitk_image = sitk.ReadImage(str(arguments.input_image))
        # Binarize: threshold > 0
        binary = sitk.Cast(sitk_image > 0, sitk.sitkUInt8)

        print("Computing connected components...")
        labeled = sitk.ConnectedComponent(binary)
        labeled_array = sitk.GetArrayFromImage(labeled)
        num_labels = int(labeled_array.max())
        print(f"Connected components: {num_labels} labels")

        output_path = Path(arguments.output_image)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        tifffile.imwrite(str(output_path), labeled_array.astype(np.uint32, copy=False))

        return self.Outputs(output_image=output_path, num_labels=num_labels)
