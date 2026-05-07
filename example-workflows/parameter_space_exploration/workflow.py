"""
Parameter Space Exploration Workflow

This workflow demonstrates combinatorial parameter testing for the Atlas spot
detection algorithm on a set of fluorescence microscopy images.

The workflow:
1. Lists input images from a directory using the Files tool.
2. Generates parameter value lists for sensitivity and size using Generate.
3. Performs a Cartesian product (cross-join) to create a full parameter grid.
4. Executes Atlas detection for each image/parameter combination.
5. Creates a mosaic visualization of all detection results.
"""

from __future__ import annotations

from bioimageflow import Workflow, configure_wetlands
from bioimageflow.node import Node
from bioimageflow_common_tools import CrossJoin, Files, Generate, Mosaic
from bioimageflow_common_tools.atlas import Atlas


def build_parameter_space_workflow(
    data_dir: str = "./data",
    storage_path: str = "./parameter_space_results",
    pattern: str = "*.tif"
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

    Returns
    -------
    Tuple[Workflow, Node]
        The workflow object and the terminal mosaic node.
    """
    wf = Workflow(storage_path=storage_path)

    # Step 1: List input images
    images = Files()(
        path=data_dir,
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
    # Note: order matters for CrossJoin; we pass images first so that the output
    # includes columns: path, filename (from images), sensitivity, size

    # Step 4: Atlas detection for each combination
    detections = Atlas()(
        input_image=param_grid["path"],
        p_value=param_grid["sensitivity"],
        gaussian_std=param_grid["size"],
        name="atlas_detections"
    )
    # Atlas produces output_image column with detection results

    # Step 5: Mosaic of all detection results. Mosaic accepts scalar image
    # semantics, so Atlas's binary detections can be visualized directly.
    mosaic = Mosaic()(
        input_image=detections["output_image"],
        columns=6,  # arrange in a 6-column grid
        name="results_mosaic"
    )

    return wf, mosaic


def main() -> None:
    import sys
    data_dir = sys.argv[1] if len(sys.argv) > 1 else "./data"
    storage_path = sys.argv[2] if len(sys.argv) > 2 else "./parameter_space_results"

    configure_wetlands(wetlands_instance_path="./wetlands")

    wf, mosaic = build_parameter_space_workflow(data_dir, storage_path)
    # Compute just the mosaic (which triggers all upstream)
    result_df = wf.compute(mosaic)
    print("Workflow complete.")
    print(f"Mosaic saved to: {result_df['mosaic_path'].iloc[0]}")
    print(f"Total images processed: {result_df['image_count'].iloc[0]}")


if __name__ == "__main__":
    main()
