"""
Cellpose 3 and StarDist Segmentation Workflow
=============================================

Runs two common 2D segmentation tools on the same input images:

  Files -> ExtractChannel(channel 2) -> Cellpose3
                                  -> StarDistSegmenter

Cellpose3 is a fast generalist cell/nuclei segmenter. StarDist is often a good
choice for round, star-convex nuclei. This example keeps the branches separate
so their masks and object counts can be inspected side by side.
"""

from __future__ import annotations

from bioimageflow import Workflow, configure_wetlands
from bioimageflow.node import Node
from bioimageflow_common_tools import ExtractChannel, Files
from bioimageflow_segmentation_tools import Cellpose3, StarDistSegmenter


def build_segmentation_workflow(
    data_dir: str = "./data",
    storage_path: str = "./segmentation_results",
    pattern: str = "*.tif",
    nuclei_channel: int = 2,
    cellpose_diameter: float = 18.0,
    engine: str = "wetlands",
    wetlands_config: dict | None = None,
) -> tuple[Workflow, Node, Node]:
    """Build a simple two-branch segmentation workflow."""

    wf = Workflow(
        storage_path=storage_path,
        engine=engine,
        wetlands_config=wetlands_config,
    )
    with wf:
        images = Files()(path=data_dir, pattern=pattern, name="input_images")
        nuclei = ExtractChannel()(
            input_image=images["path"],
            channel=nuclei_channel,
            name="nuclei_channel",
        )

        cellpose_masks = Cellpose3()(
            input_image=nuclei["output_image"],
            model_type="nuclei",
            diameter=cellpose_diameter,
            name="cellpose3_nuclei",
        )

        stardist_masks = StarDistSegmenter()(
            input_image=nuclei["output_image"],
            model_name="2D_versatile_fluo",
            name="stardist_nuclei",
        )

    return wf, cellpose_masks, stardist_masks


def main() -> None:
    import sys

    data_dir = sys.argv[1] if len(sys.argv) > 1 else "./data"
    storage_path = sys.argv[2] if len(sys.argv) > 2 else "./segmentation_results"

    configure_wetlands(wetlands_instance_path="./wetlands")

    wf, cellpose_masks, stardist_masks = build_segmentation_workflow(
        data_dir=data_dir,
        storage_path=storage_path,
    )
    results = wf.compute(cellpose_masks, stardist_masks)
    print("Workflow complete.")
    print(results["cellpose3_nuclei"][["mask", "cell_count"]])
    print(results["stardist_nuclei"][["mask", "object_count"]])


if __name__ == "__main__":
    main()
