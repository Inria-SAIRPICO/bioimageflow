"""Atlas — adaptive spot detection via external CLI."""

import subprocess
from pathlib import Path
from typing import Annotated, Any

from bioimageflow_core import (
    Arguments,
    Category,
    EnvironmentSpec,
    GUIMeta,
    ImageSpec,
    IOModel,
    Layout,
    ProcessingTool,
    Semantic,
)

atlas_env = EnvironmentSpec(
    name="atlas",
    dependencies={
        "conda": ["bioimageit::atlas"],
    },
)


class Atlas(ProcessingTool):
    """ATLAS adaptive spot detection.

    The spot size is automatically selected and the detection threshold
    adapts to the local image dynamics. Wraps the ``atlas`` CLI tool.
    """
    name = "atlas"
    documentation = (
        "ATLAS is a spot detection method. The spots size is "
        "automatically selected and the detection threshold adapts to "
        "the local image dynamics."
    )
    category = Category.SPOT_DETECTION
    tags = ["detection", "spots"]
    environment = atlas_env

    class Inputs(IOModel):
        input_image: Annotated[
            Path,
            ImageSpec(
                semantics={Semantic.INTENSITY},
                layouts={Layout.PLANAR},
                formats={"tiff"},
            ),
        ]
        gaussian_std: Annotated[
            int, GUIMeta(connectable=False, min=0, max=200, step=1)
        ] = 60
        p_value: Annotated[
            float, GUIMeta(connectable=False, min=0.0, max=1.0, step=0.000001)
        ] = 0.001
        area_lim: Annotated[
            float, GUIMeta(connectable=False, min=0.0, max=10000.0, step=0.01)
        ] = 0.0
        verbose: Annotated[bool, GUIMeta(connectable=False)] = False

    class Outputs(IOModel):
        output_image: Annotated[
            Path,
            ImageSpec(
                semantics={Semantic.BINARY},
                layouts={Layout.PLANAR},
                formats={"tiff"},
            ),
        ] = Path("{input_image.stem}_detections{ext}")

    def process_row(self, arguments: Arguments) -> Any:
        input_path = Path(arguments.input_image)
        output_path = Path(arguments.output_image)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        # Ensure blobs reference file exists
        blobs_file = Path(__file__).parent.resolve() / "data" / "blobs.txt"
        blobs_file.parent.mkdir(exist_ok=True)
        if not blobs_file.exists():
            subprocess.run(["blobsref", "-o", str(blobs_file)], check=True)

        print(f"Running Atlas spot detection on {input_path.name}...")

        command = [
            "atlas",
            "-ref", str(blobs_file),
            "-i", str(input_path),
            "-o", str(output_path),
            "-rad", str(arguments.gaussian_std),
            "-pval", str(arguments.p_value),
        ]
        if arguments.verbose:
            command.append("-v")

        subprocess.run(command, check=True)
        print(f"Atlas: detection complete -> {output_path.name}")

        return self.Outputs(output_image=output_path)
