"""nnInteractive segmentation wrapper."""

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


nninteractive_env = EnvironmentSpec(
    name="segmentation-nninteractive",
    dependencies={
        "python": "3.12",
        "pip": [
            "nninteractive==2.5.0",
            "imageio==2.37.3",
            "numpy==2.4.2",
            "tifffile==2026.3.3",
        ],
    }
)


class nnInteractive(ProcessingTool):
    """Run an nnInteractive segmentation model from point prompts."""

    display_name = "nnInteractive"
    documentation = "Run nnInteractive inference from prompt coordinates and write a label mask."
    category = Category.SEGMENTATION
    tags = ["segmentation", "nninteractive", "interactive", "deep learning"]
    environment = nninteractive_env

    class Inputs(IOModel):
        input_image: Annotated[
            Path,
            ImageSpec(semantics={Semantic.INTENSITY}, layouts={Layout.PLANAR}),
            GUIMeta(
                display_name="Input image",
                description="2D intensity image to segment.",
                connectable=Connectable.BY_DEFAULT,
            ),
        ]
        model_path: Annotated[
            Path | None,
            GUIMeta(display_name="Model path", description="Optional nnInteractive checkpoint."),
        ] = None
        prompt_points: Annotated[
            str,
            GUIMeta(
                display_name="Prompt points",
                description="Semicolon-separated y,x coordinates, for example '12,10;33,31'.",
            ),
        ] = ""

    class Outputs(IOModel):
        mask: Annotated[
            Path,
            ImageSpec(semantics={Semantic.LABEL}, layouts={Layout.PLANAR}),
            GUIMeta(display_name="Segmentation mask"),
        ] = Template("{input_image.stem}_nninteractive_mask{ext}")
        object_count: Annotated[int, GUIMeta(display_name="Object count")]

    def process_row(self, arguments: Arguments, *, context: Any = None) -> Any:
        import imageio.v3 as iio
        import numpy as np

        nninteractive = __import__("nninteractive")
        image = iio.imread(arguments.input_image)
        if hasattr(nninteractive, "predict"):
            labels = nninteractive.predict(
                image,
                model_path=arguments.model_path,
                prompt_points=_parse_points(arguments.prompt_points),
            )
        else:
            predictor = nninteractive.Predictor(model_path=arguments.model_path)
            labels = predictor.predict(image, points=_parse_points(arguments.prompt_points))

        mask_path = Path(arguments.mask)
        mask_path.parent.mkdir(parents=True, exist_ok=True)
        labels = np.asarray(labels, dtype=np.uint32)
        iio.imwrite(mask_path, labels)
        return self.Outputs(mask=mask_path, object_count=int(labels.max()))


def _parse_points(value: str) -> list[tuple[int, int]]:
    points: list[tuple[int, int]] = []
    for item in value.split(";"):
        item = item.strip()
        if not item:
            continue
        y_text, x_text = item.split(",", 1)
        points.append((int(y_text), int(x_text)))
    return points
