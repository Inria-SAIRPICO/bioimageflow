"""AverageSpotsPerNucleus — aggregate overlap DataFrames into per-image statistics."""

from typing import Annotated, Any

from bioimageflow_core import Category, GUIMeta, IOModel
from bioimageflow import DataFrameTool


class AverageSpotsPerNucleus(DataFrameTool):
    """Compute the average number of spots per nucleus from overlap tables.

    Expects two upstream overlap DataFrames (one for FOLS2, one for CSF1R),
    filters out background labels (0), groups spots by their parent nucleus,
    and computes the mean spot count per nucleus for every image in the batch.
    """
    display_name = "Average Spots Per Nucleus"
    documentation = (
        "Compute the average number of FOLS2 and CSF1R spots per nucleus "
        "from label overlap data."
    )
    category = Category.MEASUREMENT
    tags = ["statistics", "aggregation", "fish"]

    class Inputs(IOModel):
        pass

    class Outputs(IOModel):
        image_index: Annotated[str, GUIMeta(
            display_name="Image index",
            description=(
                "Identifier of the source image (copied from the DataFrame index)."
            ),
        )]
        avg_fols2_per_nucleus: Annotated[float, GUIMeta(
            display_name="Avg FOLS2 spots / nucleus",
            description=(
                "Average number of distinct FOLS2 spots overlapping each nucleus."
            ),
        )]
        avg_csf1r_per_nucleus: Annotated[float, GUIMeta(
            display_name="Avg CSF1R spots / nucleus",
            description=(
                "Average number of distinct CSF1R spots overlapping each nucleus."
            ),
        )]
        total_nuclei: Annotated[int, GUIMeta(
            display_name="Total nuclei",
            description="Number of segmented nuclei represented in the overlap table.",
        )]
        total_nuclei_fols2: Annotated[int, GUIMeta(
            display_name="Nuclei with FOLS2",
            description="Number of nuclei that overlap at least one FOLS2 spot.",
        )]
        total_nuclei_csf1r: Annotated[int, GUIMeta(
            display_name="Nuclei with CSF1R",
            description="Number of nuclei that overlap at least one CSF1R spot.",
        )]
        total_fols2_spots: Annotated[int, GUIMeta(
            display_name="Total FOLS2 spots",
            description=(
                "Total number of FOLS2 spot-nucleus associations across the image."
            ),
        )]
        total_csf1r_spots: Annotated[int, GUIMeta(
            display_name="Total CSF1R spots",
            description=(
                "Total number of CSF1R spot-nucleus associations across the image."
            ),
        )]

    def merge_dataframes(self, dfs: list[Any], arguments: Any) -> Any:
        if len(dfs) != 2:
            raise ValueError(
                "AverageSpotsPerNucleus expects two upstream overlap DataFrames: "
                "FOLS2 first, CSF1R second."
            )

        fols2 = self._summarize_overlap_dataframe(dfs[0], "fols2")
        csf1r = self._summarize_overlap_dataframe(dfs[1], "csf1r")

        result = fols2.join(csf1r, how="outer")
        for col in result.columns:
            if col.startswith("avg_"):
                result[col] = result[col].fillna(0.0)
            else:
                result[col] = result[col].fillna(0).astype(int)
        result["total_nuclei"] = result[
            ["observed_nuclei_fols2", "observed_nuclei_csf1r"]
        ].max(axis=1)

        result.index.name = None
        result["image_index"] = result.index.astype(str)
        ordered_columns = [
            "image_index",
            "avg_fols2_per_nucleus",
            "avg_csf1r_per_nucleus",
            "total_nuclei",
            "total_nuclei_fols2",
            "total_nuclei_csf1r",
            "total_fols2_spots",
            "total_csf1r_spots",
        ]
        return result[ordered_columns]

    def transform(self, df: Any, arguments: Any) -> Any:
        return df

    @staticmethod
    def _parent_index(index: Any) -> str:
        return str(index).split("::", 1)[0]

    @classmethod
    def _summarize_overlap_dataframe(cls, df: Any, label: str) -> Any:
        import pandas as pd

        required = {"reference_label", "spot_label", "overlap_count"}
        if not required.issubset(df.columns):
            missing = ", ".join(sorted(required - set(df.columns)))
            raise ValueError(f"Overlap DataFrame is missing columns: {missing}")

        work = df.copy()
        work["image_index"] = [cls._parent_index(idx) for idx in work.index]
        image_indices = pd.Index(work["image_index"].drop_duplicates().astype(str))
        observed_nuclei = (
            work.loc[work["reference_label"] > 0]
            .groupby("image_index")["reference_label"]
            .nunique()
            .rename(f"observed_nuclei_{label}")
        )

        real = work[
            (work["reference_label"] > 0)
            & (work["spot_label"] > 0)
        ]
        if real.empty:
            return pd.DataFrame(
                {
                    f"avg_{label}_per_nucleus": 0.0,
                    f"observed_nuclei_{label}": observed_nuclei.reindex(
                        image_indices, fill_value=0
                    ),
                    f"total_nuclei_{label}": 0,
                    f"total_{label}_spots": 0,
                },
                index=image_indices,
            )

        all_nuclei = (
            work.loc[work["reference_label"] > 0, ["image_index", "reference_label"]]
            .drop_duplicates()
            .set_index(["image_index", "reference_label"])
        )
        positive_spots_per_nucleus = real.groupby(
            ["image_index", "reference_label"]
        )["spot_label"].nunique()
        spots_per_nucleus = (
            all_nuclei.join(positive_spots_per_nucleus.rename("spot_count"))
            ["spot_count"]
            .fillna(0)
        )
        summary = spots_per_nucleus.groupby("image_index").agg(["mean", "sum"])
        positive_nuclei = (
            positive_spots_per_nucleus.groupby("image_index")
            .count()
            .rename(f"total_nuclei_{label}")
        )
        summary = summary.rename(
            columns={
                "mean": f"avg_{label}_per_nucleus",
                "sum": f"total_{label}_spots",
            }
        )
        summary = summary.join(positive_nuclei, how="outer")
        summary = summary.join(observed_nuclei, how="outer")
        return summary.reindex(image_indices, fill_value=0)
