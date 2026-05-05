"""ExtractChannel — extract a single channel from a multi-channel image."""

from pathlib import Path
from typing import Annotated, Any

from bioimageflow_core import (
    Arguments,
    Category,
    Connectable,
    GENERAL_ENV,
    GUIMeta,
    ImagePath,
    IOModel,
    Layout,
    ProcessingTool,
    Semantic,
    Template,
)


class ExtractChannel(ProcessingTool):
    """Extract a single channel from a multi-channel image.

    Reads an image and writes the selected channel as a 2D image.
    Expects the first axis to be the channel axis (CYX or CZYX layout).
    """
    display_name = "Extract Channel"
    documentation = "Extract a single channel (by index) from a multi-channel image."
    category = Category.IMAGE_PROCESSING
    tags = ["preprocessing", "channel"]
    environment = GENERAL_ENV

    class Inputs(IOModel):
        input_image: ImagePath(
            layouts={Layout.PLANAR_CHANNEL, Layout.VOLUMETRIC_CHANNEL},
            gui=GUIMeta(
                display_name="Input image",
                description="Multi-channel image (CYX or CZYX). The first axis is treated as the channel axis.",
                connectable=Connectable.BY_DEFAULT,
            ),
        )
        channel: Annotated[int, GUIMeta(
            display_name="Channel",
            description="Index of the channel to extract (0-based).",
            min=0, max=512, step=1,
        )] = 0

    class Outputs(IOModel):
        output_image: ImagePath(
            semantics={Semantic.INTENSITY},
            layouts={Layout.PLANAR},
            gui=GUIMeta(
                display_name="Channel image",
                description="Single-channel image containing the extracted channel.",
            ),
        ) = Template("{input_image.stem}_ch{channel}{ext}")

    def process_row(self, arguments: Arguments, *, context: Any = None) -> Any:
        import imageio.v3 as iio

        img = iio.imread(str(arguments.input_image))
        channel_data = img[arguments.channel]

        output_path = Path(arguments.output_image)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        iio.imwrite(str(output_path), channel_data)

        return self.Outputs(output_image=output_path)
