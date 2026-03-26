"""Built-in merge DataFrameTools."""

from typing import Any

import pandas as pd

from bioimageflow_core.tool import Category, IOModel
from bioimageflow.dataframe_tool import DataFrameTool, Passthrough


class InnerJoin(DataFrameTool):
    """Inner join upstream DataFrames on index (default merge behavior)."""
    name = "inner_join"
    category = Category.UTILITIES

    class Inputs(IOModel):
        pass
    # Uses default merge_dataframes (inner join on index)


class CrossJoin(DataFrameTool):
    """Cross join for combinatorial expansion."""
    name = "cross_join"
    category = Category.UTILITIES

    class Inputs(IOModel):
        suffixes: tuple[str, str] = ("_left", "_right")

    def merge_dataframes(self, dfs: list[Any], arguments: Any) -> Any:
        if not dfs:
            return pd.DataFrame()
        if len(dfs) == 1:
            return dfs[0].copy()
        suffixes: tuple[str, str] = arguments.suffixes if hasattr(arguments, 'suffixes') else ("_left", "_right")
        result = dfs[0]
        for i, df in enumerate(dfs[1:], 1):
            left_suffix = suffixes[0] if i == 1 else ""
            right_suffix = suffixes[1] if i == 1 else f"_{i+1}"
            result = result.merge(df, how="cross", suffixes=(left_suffix, right_suffix))
        return result


class JoinOnColumn(DataFrameTool):
    """Join upstream DataFrames on a named column."""
    name = "join_on_column"
    category = Category.UTILITIES

    class Inputs(IOModel):
        join_column: str
        how: str = "inner"
        suffixes: tuple[str, str] = ("_left", "_right")

    def merge_dataframes(self, dfs: list[Any], arguments: Any) -> Any:
        if not dfs:
            return pd.DataFrame()
        if len(dfs) == 1:
            return dfs[0].copy()
        result = dfs[0]
        for df in dfs[1:]:
            result = result.merge(
                df,
                on=arguments.join_column,
                how=arguments.how,
                suffixes=arguments.suffixes,
            )
        return result


class Concat(DataFrameTool):
    """Concatenate DataFrames vertically."""
    name = "concat"
    category = Category.UTILITIES

    class Inputs(IOModel):
        pass

    def merge_dataframes(self, dfs: list[Any], arguments: Any) -> Any:
        if not dfs:
            return pd.DataFrame()
        result = pd.concat(dfs)
        if result.index.duplicated().any():
            # Deduplicate to preserve :: lineage without collisions
            result = result.reset_index(drop=True)
        return result


class Collect(DataFrameTool):
    """Gather columns from multiple ancestor nodes into one DataFrame."""
    name = "collect"
    category = Category.UTILITIES

    class Outputs(Passthrough):
        pass

    class Inputs(IOModel):
        pass

    def merge_dataframes(self, dfs: list[Any], arguments: Any) -> Any:
        """Join all DataFrames on index, keeping all columns with numeric suffixes for duplicates."""
        if not dfs:
            return pd.DataFrame()
        if len(dfs) == 1:
            return dfs[0].copy()
        result = dfs[0].copy()
        for df in dfs[1:]:
            # Add numeric suffix (_1, _2, ...) for duplicate columns
            overlap_cols = set(result.columns) & set(df.columns)
            if overlap_cols:
                rename_map = {}
                for col in overlap_cols:
                    suffix = 1
                    new_name = f"{col}_{suffix}"
                    while new_name in result.columns or new_name in df.columns:
                        suffix += 1
                        new_name = f"{col}_{suffix}"
                    rename_map[col] = new_name
                df = df.rename(columns=rename_map)
            result = result.join(df, how="inner")
        return result
