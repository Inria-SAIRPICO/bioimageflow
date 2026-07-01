"""
Parameter Space Exploration Workflow

This workflow demonstrates combinatorial parameter testing for the AtlasSpotDetection
detection algorithm on a Cell Image Library FISH image.

The workflow:
1. Lists input FISH images from the shared fish_analysis data directory.
2. Generates parameter value lists for sensitivity and spot scale using Generate.
3. Performs a Cartesian product (cross-join) to create a full parameter grid.
4. Extracts the FOLS2 marker channel for every image-parameter row.
5. Executes AtlasSpotDetection for each channel image and parameter combination.
6. Creates a mosaic visualization of all detection results.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Annotated, Any

import imageio.v3 as iio
import numpy as np
import pandas as pd

from bioimageflow import DataFrameTool, Workflow, configure_wetlands
from bioimageflow.node import Node
from bioimageflow_common_tools import CrossJoin, ExtractChannel, Files, Generate, Mosaic
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
from bioimageflow_spot_tools import AtlasSpotDetection

EXAMPLE_WORKFLOWS_DIR = Path(__file__).resolve().parents[1]
DEFAULT_DATA_DIR = EXAMPLE_WORKFLOWS_DIR / "fish_analysis" / "data"
DEFAULT_STORAGE_PATH = EXAMPLE_WORKFLOWS_DIR / "outputs" / "parameter_space_exploration"


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
        foreground_fraction: Annotated[float, GUIMeta(display_name="Foreground fraction")]

    def process_row(self, arguments: Arguments, *, context: Any = None) -> Any:
        mask = np.asarray(iio.imread(arguments.input_image))
        foreground_pixels = int((mask > 0).sum())
        return self.Outputs(
            label_count=_count_foreground_components(mask),
            object_pixel_count=foreground_pixels,
            foreground_fraction=float(foreground_pixels / mask.size) if mask.size else 0.0,
        )


class ParameterSweepResults(DataFrameTool):
    """Combine parameter rows, mask paths, counts, and the mosaic preview path."""

    display_name = "Parameter Sweep Results"
    category = Category.MEASUREMENT

    class Inputs(IOModel):
        pass

    class Outputs(IOModel):
        pass

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
                detections[[column for column in detections.columns if column not in parameters.columns]],
                counts[[column for column in counts.columns if column not in parameters.columns]],
            ],
            axis=1,
        )
        if "mosaic_path" in mosaic.columns and not mosaic.empty:
            results["mosaic_path"] = mosaic["mosaic_path"].iloc[0]
        if "image_count" in mosaic.columns and not mosaic.empty:
            results["image_count"] = int(mosaic["image_count"].iloc[0])
        return results


def build_parameter_space_workflow(
    data_dir: str | Path = DEFAULT_DATA_DIR,
    storage_path: str | Path = DEFAULT_STORAGE_PATH,
    pattern: str = "13432.tif",
    marker_channel: int = 0,
    engine: str = "wetlands",
    wetlands_config: dict | None = None,
) -> tuple[Workflow, Node]:
    """
    Build the parameter space exploration workflow.

    Parameters
    ----------
    data_dir : str
        Directory containing input images.
    storage_path : str
        Directory for workflow outputs and cache.
    pattern : str
        Glob pattern for image files.
    marker_channel : int
        Channel index sent to ATLAS.

    Returns
    -------
    Tuple[Workflow, Node]
        The workflow object and the terminal mosaic node.
    """
    wf = Workflow(
        storage_path=str(storage_path),
        engine=engine,
        wetlands_config=wetlands_config,
    )
    with wf:
        # Step 1: List input images
        images = Files()(
            path=str(data_dir),
            pattern=pattern,
            name="input_images"
        )

        # Step 2: Generate parameter value lists
        sensitivity_params = Generate()(
            column_name="sensitivity",
            values=[0.001, 0.0001],
            name="sensitivity_values"
        )
        size_params = Generate()(
            column_name="size",
            values=[30, 60, 120],
            name="size_values"
        )

        # Step 3: Cartesian product of images and parameters
        param_grid = CrossJoin()(
            images,
            sensitivity_params,
            size_params,
            name="parameter_grid"
        )
        # Note: order matters for CrossJoin; we pass images first so that the
        # output includes the source path before sensitivity and size.

        # Step 4: Extract the marker channel for each concrete parameter row.
        marker_images = ExtractChannel()(
            input_image=param_grid["path"],
            channel=marker_channel,
            name="extract_marker_channel",
        )

        # Step 5: AtlasSpotDetection for each combination.
        detections = AtlasSpotDetection()(
            input_image=marker_images["output_image"],
            p_value=param_grid["sensitivity"],
            gaussian_std=param_grid["size"],
            name="atlas_detections"
        )

        # Step 6: Count connected foreground components for each ATLAS mask.
        counts = SpotMaskMetrics()(
            input_image=detections["output_image"],
            name="spot_mask_counts",
        )

        # Step 7: Mosaic of all detection results. Mosaic accepts scalar image
        # semantics, so AtlasSpotDetection's binary masks can be visualized directly.
        mosaic = Mosaic()(
            input_image=detections["output_image"],
            columns=6,
            name="results_mosaic"
        )

        results = ParameterSweepResults()(
            param_grid,
            detections,
            counts,
            mosaic,
            name="parameter_results",
        )

    return wf, results


def main() -> None:
    import sys
    data_dir = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_DATA_DIR
    storage_path = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_STORAGE_PATH

    configure_wetlands(wetlands_instance_path="./wetlands")

    wf, mosaic = build_parameter_space_workflow(data_dir, storage_path)
    # Compute just the mosaic (which triggers all upstream)
    result_df = wf.compute(mosaic)
    print("Workflow complete.")
    print(f"Mosaic saved to: {result_df['mosaic_path'].iloc[0]}")
    print(f"Total parameter rows processed: {len(result_df)}")


if __name__ == "__main__":
    main()
