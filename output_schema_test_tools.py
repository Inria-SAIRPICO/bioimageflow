"""Ordinary declarations for construction-only recursive schema controls."""

from bioimageflow import DataFrameTool
from bioimageflow.dataframe_tool import Passthrough
from bioimageflow_core import (
    EnvironmentSpec,
    IOModel,
    ImageShared,
    Layout,
    ProcessingTool,
    RowConsumption,
    Semantic,
)

callback_calls = 0


def unexpected_callback():
    global callback_calls
    callback_calls += 1
    raise AssertionError("Schema construction must not execute scientific callbacks")


class KnownImage(ProcessingTool):
    row_consumption = RowConsumption.MAPPED
    environment = EnvironmentSpec("schema-known-image", {"python": "3.12"})

    class Inputs(IOModel):
        value: int = 1

    class Outputs(IOModel):
        image: ImageShared(
            semantics=Semantic.INTENSITY, layouts=Layout.PLANAR, dtypes="uint16",
        )

    def process_row(self, arguments):
        return unexpected_callback()


class LabelConsumer(ProcessingTool):
    row_consumption = RowConsumption.MAPPED
    environment = EnvironmentSpec("schema-label-consumer", {"python": "3.12"})

    class Inputs(IOModel):
        image: ImageShared(
            semantics=Semantic.LABEL, layouts=Layout.PLANAR, dtypes="uint16",
        )

    class Outputs(IOModel):
        count: int

    def process_row(self, arguments):
        return unexpected_callback()


class UnknownColumns(DataFrameTool):
    accepts_upstream = False

    class Inputs(IOModel):
        pass

    def transform(self, df, arguments):
        return unexpected_callback()


class PartialImageColumns(DataFrameTool):
    class Inputs(IOModel):
        pass

    class Outputs(Passthrough):
        image: ImageShared(
            semantics=Semantic.INTENSITY, layouts=Layout.PLANAR, dtypes="uint16",
        )

    def transform(self, df, arguments):
        return unexpected_callback()
