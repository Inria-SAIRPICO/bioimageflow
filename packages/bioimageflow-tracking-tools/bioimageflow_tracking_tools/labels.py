"""Convert label images into object centroid tables."""

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

from ._validation import validate_label_image


class LabelsToObjects(ProcessingTool):
    """Measure object centroids and areas from 2D or TYX label images."""

    row_consumption = RowConsumption.MAPPED
    display_name = "Labels to Objects"
    documentation = "Convert label images into per-frame object centroid tables."
    category = Category.TRACKING
    tags = ["tracking", "labels", "objects"]
    environment = GENERAL_ENV

    class Inputs(IOModel):
        label_image: Annotated[
            Path,
            ImageSpec(
                semantics={Semantic.LABEL},
                layouts={Layout.PLANAR, Layout.PLANAR_TIME},
            ),
            GUIMeta(
                display_name="Label image",
                description="2D label image or TYX label stack.",
                connectable=Connectable.BY_DEFAULT,
            ),
        ]

    class Outputs(IOModel):
        frame: Annotated[int, GUIMeta(display_name="Frame")]
        label: Annotated[int, GUIMeta(display_name="Label")]
        y: Annotated[float, GUIMeta(display_name="Y")]
        x: Annotated[float, GUIMeta(display_name="X")]
        area: Annotated[int, GUIMeta(display_name="Area")]
        object_count: Annotated[int, GUIMeta(display_name="Object count")]

    def process_row(self, arguments: Arguments, *, context: Any = None) -> Any:
        import imageio.v3 as iio
        import numpy as np
        from skimage.measure import regionprops_table

        labels = iio.imread(arguments.label_image)
        validate_label_image(labels, "LabelsToObjects")
        if labels.ndim == 2:
            labels = labels[np.newaxis, ...]

        rows: list[dict[str, int | float]] = []
        for frame, plane in enumerate(labels):
            properties = regionprops_table(
                plane,
                properties=("label", "centroid", "area"),
            )
            for label, y, x, area in zip(
                properties["label"],
                properties["centroid-0"],
                properties["centroid-1"],
                properties["area"],
                strict=True,
            ):
                rows.append(
                    {
                        "frame": frame,
                        "label": int(label),
                        "y": float(y),
                        "x": float(x),
                        "area": int(area),
                    }
                )
        return [
            self.Outputs(
                frame=int(row["frame"]),
                label=int(row["label"]),
                y=float(row["y"]),
                x=float(row["x"]),
                area=int(row["area"]),
                object_count=len(rows),
            )
            for row in rows
        ]
