"""
Parameter Space Exploration Workflow

This workflow demonstrates combinatorial parameter testing for the AtlasSpotDetection
detection algorithm on a Cell Image Library FISH image.

The workflow:
1. Downloads a public FISH image into workflow-managed storage.
2. Generates parameter value lists for sensitivity and spot scale using Generate.
3. Performs a Cartesian product (cross-join) to create a full parameter grid.
4. Extracts the FOLS2 marker channel for every image-parameter row.
5. Executes AtlasSpotDetection for each channel image and parameter combination.
6. Creates a mosaic visualization of all detection results.
"""

from __future__ import annotations

from pathlib import Path

from bioimageflow import Workflow, configure_wetlands
from bioimageflow_common_tools import CrossJoin, ExtractChannel, Generate, Mosaic
from bioimageflow_spot_tools import AtlasSpotDetection
from parameter_tools.download_images import DownloadImages
from parameter_tools.metrics import ParameterSweepResults, SpotMaskMetrics

EXAMPLE_WORKFLOWS_DIR = Path(__file__).resolve().parents[1]
DEFAULT_STORAGE_PATH = EXAMPLE_WORKFLOWS_DIR / "outputs" / "parameter_space_exploration"
CIL_URL = "https://cildata.crbs.ucsd.edu/media/images/13432/13432.tif"


def build_workflow(
    *,
    storage_path: str | Path = DEFAULT_STORAGE_PATH,
    engine: str = "wetlands",
    wetlands_config: dict | None = None,
) -> Workflow:
    """
    Build the parameter space exploration workflow.

    Parameters
    ----------
    storage_path : str
        Directory for workflow outputs and cache.

    """
    wf = Workflow(
        name="parameter_space_exploration",
        display_name="Parameters Space Exploration",
        storage_path=str(storage_path),
        engine=engine,
        wetlands_config=wetlands_config,
    )
    with wf:
        marker_channel = wf.input(
            "marker_channel", int, default=0, id="input-marker-channel"
        )
        # Step 1: Download the public sample into this run's managed assets directory.
        images = DownloadImages()(
            urls=CIL_URL,
            name="download_cil_image",
        )

        # Step 2: Generate parameter value lists
        sensitivity_params = Generate()(
            column_name="sensitivity", values=[0.001, 0.0001], name="sensitivity_values"
        )
        size_params = Generate()(
            column_name="size", values=[30, 60, 120], name="size_values"
        )

        # Step 3: Cartesian product of images and parameters
        param_grid = CrossJoin()(
            images, sensitivity_params, size_params, name="parameter_grid"
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
            name="atlas_detections",
        )

        # Step 6: Count connected foreground components for each ATLAS mask.
        counts = SpotMaskMetrics()(
            input_image=detections["output_image"],
            name="spot_mask_counts",
        )

        # Step 7: Mosaic of all detection results. Mosaic accepts scalar image
        # semantics, so AtlasSpotDetection's binary masks can be visualized directly.
        mosaic = Mosaic()(
            input_image=detections["output_image"], columns=6, name="results_mosaic"
        )

        results = ParameterSweepResults()(
            param_grid,
            detections,
            counts,
            mosaic,
            name="parameter_results",
        )
        wf.output("sensitivity", results["sensitivity"], id="output-sensitivity")
        wf.output("size", results["size"], id="output-size")
        wf.output("label_count", results["label_count"], id="output-label-count")
        wf.output("mosaic_path", results["mosaic_path"], id="output-mosaic-path")
    return wf


def main() -> None:
    import sys

    storage_path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_STORAGE_PATH

    configure_wetlands(wetlands_instance_path="./wetlands")

    wf = build_workflow(storage_path=storage_path)
    result_df = wf.compute()
    print("Workflow complete.")
    print(f"Mosaic saved to: {result_df['mosaic_path'].iloc[0]}")
    print(f"Total parameter rows processed: {len(result_df)}")


if __name__ == "__main__":
    main()
