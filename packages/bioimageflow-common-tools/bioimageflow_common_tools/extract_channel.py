"""ExtractChannel — extract a single channel from a multi-channel image."""

from pathlib import Path
from typing import Annotated, Any

from bioimageflow_core import (
    Arguments,
    EnvironmentSpec,
    GUIMeta,
    ImageSpec,
    IOModel,
    Layout,
    ProcessingTool,
    Semantic,
    Template,
)

imageio_env = EnvironmentSpec(
    name="imageio",
    dependencies={"python": "3.12", "pip": ["imageio", "numpy"]},
)


class ExtractChannel(ProcessingTool):
    """Extract a single channel from a multi-channel image.

    Reads an image and writes the selected channel as a 2D image.
    Expects the first axis to be the channel axis (CYX or CZYX layout).
    """
    name = "extract_channel"
    documentation = "Extract a single channel (by index) from a multi-channel image."
    tags = ["preprocessing", "channel"]
    environment = imageio_env

    class Inputs(IOModel):
        input_image: Annotated[
            Path,
            ImageSpec(
                layouts={Layout.PLANAR_CHANNEL, Layout.VOLUMETRIC_CHANNEL},
            ),
        ]
        channel: Annotated[int, GUIMeta(connectable=False, min=0, max=512, step=1)] = 0

    class Outputs(IOModel):
        output_image: Annotated[
            Path,
            ImageSpec(
                semantics={Semantic.INTENSITY},
                layouts={Layout.PLANAR},
            ),
        ] = Template("{input_image.stem}_ch{channel}{ext}")

    def process_row(self, arguments: Arguments) -> Any:
        import imageio.v3 as iio

        img = iio.imread(str(arguments.input_image))
        channel_data = img[arguments.channel]

        output_path = Path(arguments.output_image)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        iio.imwrite(str(output_path), channel_data)

        return self.Outputs(output_image=output_path)
