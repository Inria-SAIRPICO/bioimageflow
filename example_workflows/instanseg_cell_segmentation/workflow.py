"""InstanSeg cell segmentation with region measurements."""

import argparse
from pathlib import Path

from bioimageflow import Workflow
from bioimageflow_measurement_tools import RegionProperties
from bioimageflow_segmentation_tools import InstanSegSegment


DEFAULT_STORAGE_PATH = Path(__file__).resolve().parent / "results"


def build_workflow(
    *,
    storage_path: str | Path = DEFAULT_STORAGE_PATH,
    engine: str = "wetlands",
    wetlands_config: dict | None = None,
) -> Workflow:
    """Build a cell-segmentation and measurement workflow."""
    wf = Workflow(
        name="instanseg_cell_segmentation",
        display_name="InstanSeg Cell Segmentation",
        storage_path=str(storage_path),
        engine=engine,
        wetlands_config=wetlands_config,
    )
    with wf:
        image = wf.input("input_image", Path, id="input-image")
        pixel_size = wf.input("pixel_size_um", float, default=0.5, id="input-pixel-size")
        segmentation = InstanSegSegment()(
            input_image=image,
            model_name="fluorescence_nuclei_and_cells",
            target="cells",
            pixel_size_um=pixel_size,
            processing_method="auto",
            name="instanseg_cells",
        )
        regions = RegionProperties()(
            label_image=segmentation["mask"],
            name="cell_region_properties",
        )
        wf.output("mask", segmentation["mask"], id="output-mask")
        wf.output("object_count", segmentation["object_count"], id="output-object-count")
        wf.output("model_provenance", segmentation["model_provenance"], id="output-provenance")
        wf.output("label", regions["label"], id="output-label")
        wf.output("area", regions["area"], id="output-area")
    return wf


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-image", required=True)
    parser.add_argument("--pixel-size-um", type=float, default=0.5)
    parser.add_argument("--storage-path", default=str(DEFAULT_STORAGE_PATH))
    args = parser.parse_args()
    workflow = build_workflow(storage_path=args.storage_path)
    print(
        workflow.compute(
            inputs={"input_image": args.input_image, "pixel_size_um": args.pixel_size_um}
        ).to_string(index=False)
    )
