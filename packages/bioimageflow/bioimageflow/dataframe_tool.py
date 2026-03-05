"""DataFrameTool base class — main process only."""

from typing import Any

from bioimageflow_core.tool import BaseTool, IOModel


class Passthrough(IOModel):
    """Marker base class for DataFrameTool Outputs that preserve input columns."""
    pass


class DataFrameTool(BaseTool):
    """Tool that transforms DataFrames in the main process."""

    def __call__(self, *upstream_nodes: Any, name: str | None = None, **kwargs: Any) -> Any:
        """Create a graph node. No computation occurs."""
        try:
            from bioimageflow.node import Node
        except ImportError:
            raise RuntimeError(
                f"{type(self).__name__}.__call__() requires the bioimageflow "
                f"orchestrator package."
            )
        return Node(tool=self, args=list(upstream_nodes), kwargs=kwargs, name=name)

    def merge_dataframes(self, dfs: list[Any], arguments: Any) -> Any:
        """Default: inner join on index."""
        if not dfs:
            import pandas as pd
            return pd.DataFrame()
        if len(dfs) == 1:
            return dfs[0].copy()
        result = dfs[0]
        for df in dfs[1:]:
            result = result.join(df, how="inner", rsuffix="__bif_dup")
            result = result[[c for c in result.columns if not c.endswith("__bif_dup")]]
        return result

    def transform(self, df: Any, arguments: Any) -> Any:
        """Default: identity (passthrough)."""
        return df
