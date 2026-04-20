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
        fols2_column: Annotated[str, GUIMeta(
            display_text="FOLS2 overlaps column",
            description="Name of the column holding the FOLS2.",
            connectable=Connectable.NEVER,
        )] = "overlaps"
        csfr1_column: Annotated[str, GUIMeta(
            display_text="CSF1R overlaps column",
            description="Name of the column holding the CSF1R.",
            connectable=Connectable.NEVER,
        )] = "overlaps_1"

    class Outputs(IOModel):
        image_index: Annotated[str, GUIMeta(
            display_text="Image index",
            description="Identifier of the source image (copied from the DataFrame index).",
        )]
        avg_fols2_per_nucleus: Annotated[float, GUIMeta(
            display_text="Avg FOLS2 spots / nucleus",
            description="Average number of distinct FOLS2 spots overlapping each nucleus.",
        )]
        avg_csfr1_per_nucleus: Annotated[float, GUIMeta(
            display_text="Avg CSF1R spots / nucleus",
            description="Average number of distinct CSF1R spots overlapping each nucleus.",
        )]
        total_nuclei_fols2: Annotated[int, GUIMeta(
            display_text="Nuclei with FOLS2",
            description="Number of nuclei that overlap at least one FOLS2 spot.",
        )]
        total_nuclei_csfr1: Annotated[int, GUIMeta(
            display_text="Nuclei with CSF1R",
            description="Number of nuclei that overlap at least one CSF1R spot.",
        )]
        total_fols2_spots: Annotated[int, GUIMeta(
            display_text="Total FOLS2 spots",
            description="Total number of FOLS2 spot-nucleus associations across the image.",
        )]
        total_csfr1_spots: Annotated[int, GUIMeta(
            display_text="Total CSF1R spots",
            description="Total number of CSF1R spot-nucleus associations across the image.",
        )]

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
