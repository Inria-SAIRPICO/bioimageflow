"""Workflow-local measurement tools for parameter-space exploration."""

from collections.abc import Iterable
from pathlib import Path
from typing import Annotated, Any

import numpy as np
import pandas as pd

from bioimageflow import DataFrameTool
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
    Semantic,
)


def _neighbor_offsets(ndim: int) -> Iterable[tuple[int, ...]]:
    for axis in range(ndim):
        for direction in (-1, 1):
            offset = [0] * ndim
            offset[axis] = direction
            yield tuple(offset)


def _count_foreground_components(mask: np.ndarray) -> int:
    foreground = np.asarray(mask) > 0
    visited = np.zeros(foreground.shape, dtype=bool)
    component_count = 0
    offsets = tuple(_neighbor_offsets(foreground.ndim))
    for start in zip(*np.nonzero(foreground), strict=False):
        if visited[start]:
            continue
        component_count += 1
        stack = [start]
        visited[start] = True
        while stack:
            current = stack.pop()
            for offset in offsets:
                neighbor = tuple(
                    index + delta for index, delta in zip(current, offset, strict=False)
                )
                if any(
                    index < 0 or index >= size
                    for index, size in zip(neighbor, foreground.shape, strict=False)
                ):
                    continue
                if foreground[neighbor] and not visited[neighbor]:
                    visited[neighbor] = True
                    stack.append(neighbor)
    return component_count


class SpotMaskMetrics(ProcessingTool):
    """Compute simple count and foreground metrics for ATLAS spot masks."""

    display_name = "Spot Mask Metrics"
    category = Category.MEASUREMENT
    environment = GENERAL_ENV

    class Inputs(IOModel):
        input_image: Annotated[
            Path,
            ImageSpec(semantics={Semantic.BINARY}, layouts={Layout.PLANAR}),
            GUIMeta(
                display_name="Spot mask",
                description="Binary spot mask to count.",
                connectable=Connectable.BY_DEFAULT,
            ),
        ]

    class Outputs(IOModel):
        label_count: Annotated[int, GUIMeta(display_name="Spot count")]
        object_pixel_count: Annotated[int, GUIMeta(display_name="Foreground pixels")]
        foreground_fraction: Annotated[
            float, GUIMeta(display_name="Foreground fraction")
        ]

    def process_row(self, arguments: Arguments, *, context: Any = None) -> Any:
        import imageio.v3 as iio

        mask = np.asarray(iio.imread(arguments.input_image))
        foreground_pixels = int((mask > 0).sum())
        return self.Outputs(
            label_count=_count_foreground_components(mask),
            object_pixel_count=foreground_pixels,
            foreground_fraction=float(foreground_pixels / mask.size)
            if mask.size
            else 0.0,
        )


class ParameterSweepResults(DataFrameTool):
    """Combine parameter rows, mask paths, counts, and the mosaic preview path."""

    display_name = "Parameter Sweep Results"
    category = Category.MEASUREMENT

    class Inputs(IOModel):
        pass

    class Outputs(IOModel):
        sensitivity: float
        size: int
        label_count: int
        mosaic_path: str

    def merge_dataframes(self, dfs: list[Any], arguments: Any) -> pd.DataFrame:
        if len(dfs) != 4:
            raise ValueError(
                "ParameterSweepResults expects parameter, detection, count, and mosaic tables."
            )
        parameters = pd.DataFrame(dfs[0]).reset_index(drop=True)
        detections = pd.DataFrame(dfs[1]).reset_index(drop=True)
        counts = pd.DataFrame(dfs[2]).reset_index(drop=True)
        mosaic = pd.DataFrame(dfs[3])
        results = pd.concat(
            [
                parameters,
                detections[
                    [
                        column
                        for column in detections.columns
                        if column not in parameters.columns
                    ]
                ],
                counts[
                    [
                        column
                        for column in counts.columns
                        if column not in parameters.columns
                    ]
                ],
            ],
            axis=1,
        )
        if "mosaic_path" in mosaic.columns and not mosaic.empty:
            results["mosaic_path"] = mosaic["mosaic_path"].iloc[0]
        if "image_count" in mosaic.columns and not mosaic.empty:
            results["image_count"] = int(mosaic["image_count"].iloc[0])
        return results
