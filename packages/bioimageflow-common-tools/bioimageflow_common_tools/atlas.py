"""Atlas — adaptive spot detection via external CLI."""

import subprocess
import tempfile
from pathlib import Path
from typing import Annotated, Any

from bioimageflow_core import (
    Arguments,
    Category,
    Connectable,
    EnvironmentSpec,
    ExecutionContext,
    GUIMeta,
    ImagePath,
    IOModel,
    Layout,
    ProcessingTool,
    Semantic,
    Template,
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
    display_name = "Atlas"
    documentation = (
        "ATLAS is a spot detection method. The spots size is "
        "automatically selected and the detection threshold adapts to "
        "the local image dynamics."
    )
    category = Category.SPOT_DETECTION
    tags = ["detection", "spots"]
    environment = atlas_env

    class Inputs(IOModel):
        input_image: ImagePath(
            semantics={Semantic.INTENSITY},
            layouts={Layout.PLANAR},
            formats={"tiff"},
            gui=GUIMeta(
                display_name="Input image",
                description="2D intensity TIFF image on which to detect spots.",
                connectable=Connectable.BY_DEFAULT,
            ),
        )
        gaussian_std: Annotated[int | None, GUIMeta(
            display_name="Gaussian std",
            description="Standard deviation (in pixels) of the Gaussian kernel used to approximate spot size. Leave unset to use Atlas's built-in default.",
            min=0, max=200, step=1,
        )] = None
        p_value: Annotated[float | None, GUIMeta(
            display_name="P-value",
            description="Detection significance threshold. Lower values yield fewer, more confident detections. Leave unset to use Atlas's built-in default.",
            min=0.0, max=1.0, step=0.000001,
        )] = None
        area_lim: Annotated[float | None, GUIMeta(
            display_name="Area limit",
            description="Remove detections smaller than this area (in pixels). Leave unset to use Atlas's built-in default.",
            min=0.0, max=10000.0, step=0.01,
        )] = None
        verbose: Annotated[bool, GUIMeta(
            display_name="Verbose",
            description="Print detailed progress information from the Atlas CLI.",
            connectable=Connectable.NEVER,
        )] = False

    class Outputs(IOModel):
        output_image: ImagePath(
            semantics={Semantic.BINARY},
            layouts={Layout.PLANAR},
            formats={"tiff"},
            gui=GUIMeta(
                display_name="Detections",
                description="Binary mask of detected spots (non-zero pixels mark spot locations).",
            ),
        ) = Template("{input_image.stem}_detections{ext}")

    def process_row(
        self,
        arguments: Arguments,
        *,
        context: ExecutionContext | None = None,
    ) -> Any:
        input_path = Path(arguments.input_image)
        output_path = Path(arguments.output_image)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        temp_dir: tempfile.TemporaryDirectory[str] | None = None
        if context is None:
            temp_dir = tempfile.TemporaryDirectory(prefix="bioimageflow_atlas_")
            work_dir = Path(temp_dir.name)
        else:
            work_dir = context.work_dir
        work_dir.mkdir(parents=True, exist_ok=True)

        # Use packaged static data when available. If a development checkout is
        # missing it, generate the fallback into execution scratch, not package data.
        blobs_file = Path(__file__).parent.resolve() / "data" / "blobs.txt"
        if not blobs_file.exists():
            blobs_file = work_dir / "blobs.txt"
            subprocess.run(["blobsref", "-o", str(blobs_file)], check=True, cwd=work_dir)

        try:
            print(f"Running Atlas spot detection on {input_path.name}...")

            command = [
                "atlas",
                "-ref", str(blobs_file),
                "-i", str(input_path),
                "-o", str(output_path),
            ]
            if arguments.gaussian_std is not None:
                command += ["-rad", str(arguments.gaussian_std)]
            if arguments.p_value is not None:
                command += ["-pval", str(arguments.p_value)]
            if arguments.area_lim is not None:
                command += ["-arealim", str(arguments.area_lim)]
            if arguments.verbose:
                command.append("-v")

            subprocess.run(command, check=True, cwd=work_dir)
            print(f"Atlas: detection complete -> {output_path.name}")

            return self.Outputs(output_image=output_path)
        finally:
            if temp_dir is not None:
                temp_dir.cleanup()
