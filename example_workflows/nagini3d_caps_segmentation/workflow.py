"""NAGINI-3D segmentation and parametric-surface summary workflow."""

import argparse
from pathlib import Path
from typing import Annotated, Any

from bioimageflow import Workflow
from bioimageflow_core import (
    Arguments,
    Category,
    Connectable,
    GENERAL_ENV,
    GUIMeta,
    IOModel,
    ProcessingTool,
    RowConsumption,
)
from bioimageflow_segmentation_tools import Nagini3DSegment


DEFAULT_STORAGE_PATH = Path(__file__).resolve().parent / "results"


class NaginiSurfaceSummary(ProcessingTool):
    """Summarize centers and curvature from a NAGINI surface archive."""

    row_consumption = RowConsumption.MAPPED
    display_name = "NAGINI Surface Summary"
    category = Category.MEASUREMENT
    environment = GENERAL_ENV

    class Inputs(IOModel):
        surfaces: Annotated[
            Path,
            GUIMeta(display_name="Surface archive", connectable=Connectable.BY_DEFAULT),
        ]

    class Outputs(IOModel):
        surface_index: Annotated[int, GUIMeta(display_name="Surface index")]
        center_z: Annotated[float, GUIMeta(display_name="Center Z")]
        center_y: Annotated[float, GUIMeta(display_name="Center Y")]
        center_x: Annotated[float, GUIMeta(display_name="Center X")]
        mean_abs_curvature: Annotated[float, GUIMeta(display_name="Mean absolute curvature")]

    def process_row(self, arguments: Arguments, *, context: Any = None) -> Any:
        import numpy as np

        with np.load(arguments.surfaces) as archive:
            centers = np.asarray(archive["centers"], dtype=float)
            curvature = np.asarray(archive["curvature_values"], dtype=float)
        if centers.ndim != 2 or centers.shape[1] != 3:
            raise ValueError("NAGINI centers must have shape (objects, 3).")
        if len(curvature) != len(centers):
            raise ValueError("NAGINI curvature data must contain one entry per surface.")
        return [
            self.Outputs(
                surface_index=index + 1,
                center_z=float(center[0]),
                center_y=float(center[1]),
                center_x=float(center[2]),
                mean_abs_curvature=float(np.nanmean(np.abs(curvature[index]))),
            )
            for index, center in enumerate(centers)
        ]


def build_workflow(
    *,
    storage_path: str | Path = DEFAULT_STORAGE_PATH,
    engine: str = "wetlands",
    wetlands_config: dict | None = None,
) -> Workflow:
    """Build the NAGINI-3D CAPS workflow."""
    wf = Workflow(
        name="nagini3d_caps_segmentation",
        display_name="NAGINI-3D CAPS Segmentation",
        storage_path=str(storage_path),
        engine=engine,
        wetlands_config=wetlands_config,
    )
    with wf:
        volume = wf.input("input_volume", Path, id="input-volume")
        model_bundle = wf.input("model_bundle", Path, id="input-model-bundle")
        segmentation = Nagini3DSegment()(
            input_volume=volume,
            model_bundle=model_bundle,
            name="nagini3d_segmentation",
        )
        summaries = NaginiSurfaceSummary()(
            surfaces=segmentation["surfaces"],
            name="surface_curvature_summary",
        )
        wf.output("mask", segmentation["mask"], id="output-mask")
        wf.output("probability", segmentation["probability"], id="output-probability")
        wf.output("surfaces", segmentation["surfaces"], id="output-surfaces")
        wf.output("model_provenance", segmentation["model_provenance"], id="output-provenance")
        wf.output("surface_index", summaries["surface_index"], id="output-surface-index")
        wf.output("mean_abs_curvature", summaries["mean_abs_curvature"], id="output-curvature")
    return wf


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-volume", required=True)
    parser.add_argument("--model-bundle", required=True)
    parser.add_argument("--storage-path", default=str(DEFAULT_STORAGE_PATH))
    args = parser.parse_args()
    workflow = build_workflow(storage_path=args.storage_path)
    print(
        workflow.compute(
            inputs={
                "input_volume": args.input_volume,
                "model_bundle": args.model_bundle,
            }
        ).to_string(index=False)
    )
