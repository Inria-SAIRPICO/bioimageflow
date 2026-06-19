"""Cell counting and phenotyping workflow on a tiny BBBC038-style fixture."""

from pathlib import Path
from typing import Annotated, Any

import imageio.v3 as iio
import numpy as np
import pandas as pd

from bioimageflow import DataFrameTool, Workflow
from bioimageflow.node import Node
from bioimageflow_core import Category, GUIMeta, IOModel
from bioimageflow_measurement_tools import RegionProperties
from bioimageflow_segmentation_tools import ThresholdSegment


def _write_fixture(data_dir: Path) -> Path:
    data_dir.mkdir(parents=True, exist_ok=True)
    yy, xx = np.mgrid[0:64, 0:64]
    image = np.zeros((64, 64), dtype=np.float32)
    for cy, cx, radius, value in [(18, 18, 7, 0.8), (42, 25, 9, 1.0), (36, 47, 6, 0.7)]:
        image[(yy - cy) ** 2 + (xx - cx) ** 2 <= radius**2] = value
    path = data_dir / "synthetic_cell_counting_image.tif"
    iio.imwrite(path, image)
    return path


class PhenotypeSummary(DataFrameTool):
    """Aggregate region rows into one per-image phenotype row."""

    display_name = "Phenotype Summary"
    category = Category.MEASUREMENT

    class Inputs(IOModel):
        image_name: Annotated[str, GUIMeta(display_name="Image name")] = "synthetic_cell_counting_image"

    class Outputs(IOModel):
        image: Annotated[str, GUIMeta(display_name="Image")]
        object_count: Annotated[int, GUIMeta(display_name="Object count")]
        mean_area: Annotated[float, GUIMeta(display_name="Mean area")]
        total_area: Annotated[float, GUIMeta(display_name="Total area")]
        mean_centroid_y: Annotated[float, GUIMeta(display_name="Mean centroid Y")]
        mean_centroid_x: Annotated[float, GUIMeta(display_name="Mean centroid X")]

    def transform(self, df: Any, arguments: Any) -> pd.DataFrame:
        table = pd.DataFrame(df)
        if table.empty:
            return pd.DataFrame(
                [{
                    "image": arguments.image_name,
                    "object_count": 0,
                    "mean_area": 0.0,
                    "total_area": 0.0,
                    "mean_centroid_y": 0.0,
                    "mean_centroid_x": 0.0,
                }]
            )
        return pd.DataFrame(
            [{
                "image": arguments.image_name,
                "object_count": int(len(table)),
                "mean_area": float(table["area"].mean()),
                "total_area": float(table["area"].sum()),
                "mean_centroid_y": float(table["centroid_y"].mean()),
                "mean_centroid_x": float(table["centroid_x"].mean()),
            }]
        )


def build_workflow(
    storage_path: str = "./cell_counting_phenotyping_results",
    engine: str = "wetlands",
    wetlands_config: dict | None = None,
) -> tuple[Workflow, Node]:
    """Build segmentation -> regionprops -> per-image phenotype summary."""
    storage = Path(storage_path)
    image_path = _write_fixture(storage / "data")

    wf = Workflow(
        storage_path=str(storage / "bif"),
        engine=engine,
        wetlands_config=wetlands_config,
    )
    with wf:
        labels = ThresholdSegment()(
            input_image=image_path,
            threshold=0.5,
            name="segment_cells",
        )
        regions = RegionProperties()(
            label_image=labels["labels"],
            name="measure_regions",
        )
        summary = PhenotypeSummary()(
            regions,
            image_name=image_path.stem,
            name="summarize_phenotypes",
        )
    return wf, summary


if __name__ == "__main__":
    workflow, terminal = build_workflow()
    print(workflow.compute(terminal).to_string(index=False))
