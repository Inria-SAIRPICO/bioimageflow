"""Generate — create a DataFrame from a list of values."""

from typing import Annotated, Any

from bioimageflow_core import Category, Connectable, GUIMeta, IOModel
from bioimageflow import DataFrameTool


class Generate(DataFrameTool):
    """Generate a DataFrame containing a single column of values."""
    display_name = "Generate"
    documentation = (
        "Creates a DataFrame with one column from a list of values. "
        "Useful for parameter value generation in combinatorial workflows."
    )
    category = Category.UTILITIES
    tags = ["dataframe", "generator"]

    class Inputs(IOModel):
        column_name: Annotated[str, GUIMeta(connectable=Connectable.NEVER)]
        values: Annotated[list[Any], GUIMeta(connectable=Connectable.NEVER)]

    def transform(self, df, arguments):
        import pandas as pd
        return pd.DataFrame({arguments.column_name: arguments.values})
