"""ConnectedComponents — label connected components in a binary image."""

from pathlib import Path
from typing import Annotated, Any

from bioimageflow_core import (
    Arguments,
    Category,
    EnvironmentSpec,
    ImageSpec,
    IOModel,
    Layout,
    ProcessingTool,
    Semantic,
)

simpleitk_env = EnvironmentSpec(
    name="simpleitk",
    dependencies={"python": "3.12", "pip": ["SimpleITK", "numpy"]},
)


class ConnectedComponents(ProcessingTool):
    """Label connected components in a binary image using SimpleITK.

    Converts a binary detection map into a labeled image where each
    connected region receives a unique integer identifier.
    Uses SimpleITK for both I/O and processing.
    """
    name = "connected_components"
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
        ]

    class Outputs(IOModel):
        output_image: Annotated[
            Path,
            ImageSpec(
                semantics={Semantic.LABEL},
                layouts={Layout.PLANAR, Layout.VOLUMETRIC},
            ),
        ] = Path("{input_image.stem}_labels{ext}")
        num_labels: int

    def process_row(self, arguments: Arguments) -> Any:
        import SimpleITK as sitk    # type: ignore

        sitk_image = sitk.ReadImage(str(arguments.input_image))
        # Binarize: threshold > 0
        binary = sitk.Cast(sitk_image > 0, sitk.sitkUInt8)

        labeled = sitk.ConnectedComponent(binary)
        labeled_array = sitk.GetArrayFromImage(labeled)
        num_labels = int(labeled_array.max())

        output_path = Path(arguments.output_image)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        sitk.WriteImage(sitk.Cast(labeled, sitk.sitkUInt16), str(output_path))

        return self.Outputs(output_image=output_path, num_labels=num_labels)
