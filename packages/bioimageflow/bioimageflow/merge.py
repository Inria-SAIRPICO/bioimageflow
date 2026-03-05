"""Built-in merge DataFrameTools."""

from typing import Any, ClassVar

import pandas as pd

from bioimageflow_core.tool import IOModel
from bioimageflow.dataframe_tool import DataFrameTool, Passthrough


class InnerJoin(DataFrameTool):
    """Inner join upstream DataFrames on index."""
    name = "inner_join"

    class Inputs(IOModel):
        pass

    def merge_dataframes(self, dfs: list[Any], arguments: Any) -> Any:
        if not dfs:
            return pd.DataFrame()
        if len(dfs) == 1:
            return dfs[0].copy()
        result = dfs[0]
        for df in dfs[1:]:
            result = result.join(df, how="inner", rsuffix="__bif_dup")
            result = result[[c for c in result.columns if not c.endswith("__bif_dup")]]
        return result


class CrossJoin(DataFrameTool):
    """Cross join for combinatorial expansion."""
    name = "cross_join"

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

    class Inputs(IOModel):
        pass

    def merge_dataframes(self, dfs: list[Any], arguments: Any) -> Any:
        if not dfs:
            return pd.DataFrame()
        return pd.concat(dfs, ignore_index=True)


class Collect(DataFrameTool):
    """Gather columns from multiple ancestor nodes into one DataFrame."""
    name = "collect"

    class Outputs(Passthrough):
        pass

    class Inputs(IOModel):
        pass
