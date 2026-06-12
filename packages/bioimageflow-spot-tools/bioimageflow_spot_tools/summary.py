"""Summarize spot assignments per label."""

from typing import Annotated, Any

from bioimageflow import DataFrameTool
from bioimageflow_core import (
    Arguments,
    Category,
    GUIMeta,
    IOModel,
)


class SpotSummary(DataFrameTool):
    """Aggregate assigned puncta counts and intensities by label."""

    display_name = "Spot Summary"
    documentation = "Compute per-label spot count and intensity summaries."
    category = Category.MEASUREMENT
    tags = ["spots", "summary", "puncta"]

    class Inputs(IOModel):
        label_column: Annotated[
            str,
            GUIMeta(
                display_name="Label column",
                description="Column containing assigned label IDs.",
            ),
        ] = "label"
        intensity_column: Annotated[
            str,
            GUIMeta(
                display_name="Intensity column",
                description="Column containing spot intensities.",
            ),
        ] = "intensity"

    class Outputs(IOModel):
        label: Annotated[int, GUIMeta(display_name="Label")]
        spot_count: Annotated[int, GUIMeta(display_name="Spot count")]
        mean_intensity: Annotated[float, GUIMeta(display_name="Mean intensity")]
        total_intensity: Annotated[float, GUIMeta(display_name="Total intensity")]
        label_count: Annotated[int, GUIMeta(display_name="Label count")]

    def merge_dataframes(self, dfs: list[Any], arguments: Arguments) -> Any:
        if len(dfs) != 1:
            raise ValueError("SpotSummary requires exactly one upstream spot table.")
        return dfs[0].copy()

    def transform(self, df: Any, arguments: Arguments) -> Any:
        import pandas as pd

        label_column = getattr(arguments, "label_column", "label")
        intensity_column = getattr(arguments, "intensity_column", "intensity")
        missing = [
            column
            for column in (label_column, intensity_column)
            if column not in df.columns
        ]
        if missing:
            raise ValueError(
                "SpotSummary input table is missing required column(s): "
                + ", ".join(repr(column) for column in missing)
                + "."
            )

        table = df[[label_column, intensity_column]].copy()
        table[label_column] = pd.to_numeric(table[label_column])
        table[intensity_column] = pd.to_numeric(table[intensity_column])
        table = table[table[label_column] > 0]
        grouped = table.groupby(label_column, sort=True)[intensity_column]
        result = grouped.agg(
            spot_count="count",
            mean_intensity="mean",
            total_intensity="sum",
        ).reset_index()
        result = result.rename(columns={label_column: "label"})
        result["label"] = result["label"].astype(int)
        result["spot_count"] = result["spot_count"].astype(int)
        result["label_count"] = len(result)
        result.index = [str(label) for label in result["label"]]
        return result[
            ["label", "spot_count", "mean_intensity", "total_intensity", "label_count"]
        ]
