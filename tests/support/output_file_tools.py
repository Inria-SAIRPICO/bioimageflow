"""Small ordinary imported tools proving pre-dispatch exact-output ownership."""
from pathlib import Path
import pandas as pd
from bioimageflow import DataFrameTool
from bioimageflow_core import IOModel


class ImageRows(DataFrameTool):
    accepts_upstream = False
    class Inputs(IOModel):
        paths: list[str]
    class Outputs(IOModel):
        image: Path
        threshold: int
    def transform(self, df, arguments):
        return pd.DataFrame({"image": [Path(path) for path in arguments.paths], "threshold": range(len(arguments.paths))}, index=[str(i) for i in range(len(arguments.paths))])


