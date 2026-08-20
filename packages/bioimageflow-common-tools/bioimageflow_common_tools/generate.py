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
    accepts_upstream = False

    class Inputs(IOModel):
        column_name: Annotated[str, GUIMeta(
            display_name="Column name",
            description="Name of the single column produced in the output DataFrame.",
            connectable=Connectable.NEVER,
        )]
        values: Annotated[list[Any], GUIMeta(
            display_name="Values",
            description="List of values that become the rows of the generated column.",
            connectable=Connectable.NEVER,
        )]

    @classmethod
    def resolve_outputs(cls, inputs=None):
        name = (inputs or {}).get("column_name")
        if not isinstance(name, str) or not name.strip():
            return None
        return {name: {"type": "any", "default": None, "image_spec": None}}

    def transform(self, df, arguments):
        import pandas as pd

        name = arguments.column_name
        if not isinstance(name, str) or not name.strip():
            raise ValueError("Column name must be a non-empty string.")
        return pd.DataFrame({name: arguments.values})
