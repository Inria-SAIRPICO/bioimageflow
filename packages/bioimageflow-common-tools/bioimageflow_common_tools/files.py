"""Files — source DataFrameTool that lists image files in a directory."""

from pathlib import Path
from typing import Any

from bioimageflow_core import IOModel
from bioimageflow import DataFrameTool


class Files(DataFrameTool):
    """List image files in a directory. Acts as a workflow source node."""
    name = "files"
    documentation = (
        "List files in a directory matching a glob pattern. "
        "Produces a DataFrame with 'path' and 'filename' columns."
    )
    tags = ["source", "loader"]

    class Inputs(IOModel):
        path: str
        pattern: str = "*"

    class Outputs(IOModel):
        path: Path
        filename: str

    def transform(self, df: Any, arguments: Any) -> Any:
        import pandas as pd

        directory = Path(arguments.path)
        files = sorted(directory.glob(arguments.pattern))
        rows = [
            {"path": str(f), "filename": f.name}
            for f in files
            if f.is_file()
        ]
        return pd.DataFrame(rows)
