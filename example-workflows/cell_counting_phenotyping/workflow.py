"""Cell counting and phenotyping workflow for a microscopy crop."""

import argparse
from pathlib import Path
from typing import Annotated, Any

import pandas as pd

from bioimageflow import DataFrameTool, Workflow
from bioimageflow.node import Node
from bioimageflow_common_tools import JoinOnColumn
from bioimageflow_core import Category, GUIMeta, IOModel
from bioimageflow_measurement_tools import (
    IntensityProperties,
    RegionProperties,
    ShapeProperties,
)
from bioimageflow_segmentation_tools import ThresholdSegment


class PhenotypeSummary(DataFrameTool):
    """Aggregate region rows into one per-image phenotype row."""

    display_name = "Phenotype Summary"
    category = Category.MEASUREMENT

    class Inputs(IOModel):
        image_name: Annotated[str, GUIMeta(display_name="Image name")]

    class Outputs(IOModel):
        image: Annotated[str, GUIMeta(display_name="Image")]
        object_count: Annotated[int, GUIMeta(display_name="Object count")]
        mean_area: Annotated[float, GUIMeta(display_name="Mean area")]
        total_area: Annotated[float, GUIMeta(display_name="Total area")]
        mean_centroid_y: Annotated[float, GUIMeta(display_name="Mean centroid Y")]
        mean_centroid_x: Annotated[float, GUIMeta(display_name="Mean centroid X")]
        mean_intensity: Annotated[float, GUIMeta(display_name="Mean intensity")]
        mean_perimeter: Annotated[float, GUIMeta(display_name="Mean perimeter")]
        mean_equivalent_diameter: Annotated[
            float,
            GUIMeta(display_name="Mean equivalent diameter"),
        ]

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
                    "mean_intensity": 0.0,
                    "mean_perimeter": 0.0,
                    "mean_equivalent_diameter": 0.0,
                }]
            )
        area_column = "area_left" if "area_left" in table.columns else "area"
        return pd.DataFrame(
            [{
                "image": arguments.image_name,
                "object_count": int(len(table)),
                "mean_area": float(table[area_column].mean()),
                "total_area": float(table[area_column].sum()),
                "mean_centroid_y": float(table["centroid_y"].mean()),
                "mean_centroid_x": float(table["centroid_x"].mean()),
                "mean_intensity": float(table["mean_intensity"].mean()),
                "mean_perimeter": float(table["perimeter"].mean()),
                "mean_equivalent_diameter": float(table["equivalent_diameter"].mean()),
            }]
        )


def build_workflow(
    input_image: str | None = None,
    storage_path: str = "./cell_counting_phenotyping_results",
    engine: str = "wetlands",
    wetlands_config: dict | None = None,
) -> tuple[Workflow, Node]:
    """Build segmentation -> regionprops -> per-image phenotype summary."""
    if input_image is None:
        raise ValueError("build_workflow requires input_image.")
    storage = Path(storage_path)
    image_path = Path(input_image)

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
        shapes = ShapeProperties()(
            label_image=labels["labels"],
            name="measure_shape_features",
        )
        intensities = IntensityProperties()(
            label_image=labels["labels"],
            intensity_image=image_path,
            name="measure_intensity_features",
        )
        region_shapes = JoinOnColumn()(
            regions,
            shapes,
            join_column="label",
            name="region_shape_table",
        )
        features = JoinOnColumn()(
            region_shapes,
            intensities,
            join_column="label",
            name="object_feature_table",
        )
        summary = PhenotypeSummary()(
            features,
            image_name=image_path.stem,
            name="summarize_phenotypes",
        )
    return wf, summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-image", required=True, help="2D microscopy crop to segment and measure.")
    parser.add_argument(
        "--storage-path",
        default="./cell_counting_phenotyping_results",
        help="Directory for workflow outputs.",
    )
    args = parser.parse_args()
    workflow, terminal = build_workflow(
        input_image=args.input_image,
        storage_path=args.storage_path,
    )
    print(workflow.compute(terminal).to_string(index=False))
