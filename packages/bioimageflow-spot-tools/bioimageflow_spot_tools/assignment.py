"""Assign detected spots to label images."""

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
)

from .validation import finite_float, label_array, pixel_coordinate, positive_uint32_id


class AssignSpotsToLabels(ProcessingTool):
    """Assign each spot coordinate to the label value at the same pixel."""

    row_consumption = RowConsumption.MAPPED
    display_name = "Assign Spots to Labels"
    documentation = "Assign detected spot coordinates to nuclei or cell label images."
    category = Category.MEASUREMENT
    tags = ["spots", "labels", "assignment"]
    environment = GENERAL_ENV

    class Inputs(IOModel):
        spot_id: Annotated[
            int,
            GUIMeta(
                display_name="Spot ID",
                description="Spot identifier from a spot detection table.",
                connectable=Connectable.BY_DEFAULT,
            ),
        ]
        y: Annotated[
            float, GUIMeta(display_name="Y", connectable=Connectable.BY_DEFAULT)
        ]
        x: Annotated[
            float, GUIMeta(display_name="X", connectable=Connectable.BY_DEFAULT)
        ]
        intensity: Annotated[
            float,
            GUIMeta(display_name="Intensity", connectable=Connectable.BY_DEFAULT),
        ]
        score: Annotated[
            float,
            GUIMeta(display_name="Score", connectable=Connectable.BY_DEFAULT),
        ] = 0.0
        label_image: Annotated[
            Path,
            ImageSpec(semantics={Semantic.LABEL}, layouts={Layout.PLANAR}),
            GUIMeta(
                display_name="Label image",
                description="2D label image used for spot assignment.",
                connectable=Connectable.BY_DEFAULT,
            ),
        ]

    class Outputs(IOModel):
        spot_id: Annotated[int, GUIMeta(display_name="Spot ID")]
        y: Annotated[float, GUIMeta(display_name="Y")]
        x: Annotated[float, GUIMeta(display_name="X")]
        intensity: Annotated[float, GUIMeta(display_name="Intensity")]
        score: Annotated[float, GUIMeta(display_name="Score")]
        label: Annotated[int, GUIMeta(display_name="Assigned label")]
        assigned_count: Annotated[int, GUIMeta(display_name="Assigned spot count")]

    def process_row(self, arguments: Arguments, *, context: Any = None) -> Any:
        import imageio.v3 as iio

        labels = label_array(iio.imread(arguments.label_image))
        y, x, y_pixel, x_pixel = pixel_coordinate(
            arguments.y,
            arguments.x,
            labels.shape,
        )
        spot_id = positive_uint32_id(arguments.spot_id)
        intensity = finite_float(arguments.intensity, "intensity")
        score = finite_float(getattr(arguments, "score", 0.0), "score")
        label = int(labels[y_pixel, x_pixel])
        return self.Outputs(
            spot_id=spot_id,
            y=y,
            x=x,
            intensity=intensity,
            score=score,
            label=label,
            assigned_count=1 if label > 0 else 0,
        )
