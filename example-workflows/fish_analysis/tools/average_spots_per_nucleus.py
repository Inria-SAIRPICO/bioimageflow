"""AverageSpotsPerNucleus — aggregate overlap DataFrames into per-image statistics."""

from typing import Annotated, Any

from bioimageflow_core import Category, Connectable, GUIMeta, IOModel
from bioimageflow import DataFrameTool


class AverageSpotsPerNucleus(DataFrameTool):
    """Compute the average number of spots per nucleus from overlap CSVs.

    Expects two upstream overlap DataFrames (one for FOLS2, one for CSF1R),
    merged via Collect. Reads the overlap CSV files produced by
    LabelOverlaps, filters out background labels (0), groups spots by
    their parent nucleus, and computes the mean spot count per nucleus
    for every image in the batch.
    """
    display_name = "Average Spots Per Nucleus"
    documentation = (
        "Compute the average number of FOLS2 and CSF1R spots per nucleus "
        "from label overlap data."
    )
    category = Category.MEASUREMENT
    tags = ["statistics", "aggregation", "fish"]

    class Inputs(IOModel):
        fols2_column: Annotated[str, GUIMeta(connectable=Connectable.NEVER)] = "overlaps"
        csfr1_column: Annotated[str, GUIMeta(connectable=Connectable.NEVER)] = "overlaps_1"

    class Outputs(IOModel):
        image_index: str
        avg_fols2_per_nucleus: float
        avg_csfr1_per_nucleus: float
        total_nuclei_fols2: int
        total_nuclei_csfr1: int
        total_fols2_spots: int
        total_csfr1_spots: int

    def transform(self, df: Any, arguments: Any) -> Any:
        import pandas as pd

        fols2_col = arguments.fols2_column
        csfr1_col = arguments.csfr1_column

        rows = []
        for idx in df.index:
            row_result: dict[str, Any] = {"image_index": str(idx)}

            for label, col in [("fols2", fols2_col), ("csfr1", csfr1_col)]:
                overlap_path = df.at[idx, col]
                overlap_df = pd.read_csv(str(overlap_path))

                # Filter out background (label 0)
                overlap_df = overlap_df[
                    (overlap_df["reference_label"] > 0)
                    & (overlap_df["spot_label"] > 0)
                ]

                if overlap_df.empty:
                    row_result[f"avg_{label}_per_nucleus"] = 0.0
                    row_result[f"total_nuclei_{label}"] = 0
                    row_result[f"total_{label}_spots"] = 0
                    continue

                # Count unique spots per nucleus
                spots_per_nucleus = (
                    overlap_df.groupby("reference_label")["spot_label"].nunique()
                )

                row_result[f"avg_{label}_per_nucleus"] = float(
                    spots_per_nucleus.mean()
                )
                row_result[f"total_nuclei_{label}"] = len(spots_per_nucleus)
                row_result[f"total_{label}_spots"] = int(spots_per_nucleus.sum())

            rows.append(row_result)

        return pd.DataFrame(rows)
