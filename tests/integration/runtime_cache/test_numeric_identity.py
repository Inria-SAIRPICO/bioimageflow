"""Exact public scientific values and independently specified identity facts."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from bioimageflow import DataFrameTool, Workflow
from bioimageflow.cache import compute_env_hash
from bioimageflow.storage import canonical_dataframe_digest
from bioimageflow_core import EnvironmentSpec, IOModel, ProcessingTool, RowConsumption


class ExactNumericSource(DataFrameTool):
    accepts_upstream = False

    class Inputs(IOModel):
        identifier: str = "001"
        integer: int = 2**63 + 5

    class Outputs(IOModel):
        identifier: str
        integer: int
        measurement: float

    def transform(self, df, arguments):
        return pd.DataFrame(
            {
                "identifier": [arguments.identifier],
                "integer": pd.Series(
                    [arguments.integer], index=["row"], dtype="uint64"
                ),
                "measurement": [0.5],
            },
            index=["row"],
        )


class EchoExactNumeric(ProcessingTool):
    row_consumption = RowConsumption.MAPPED
    environment = EnvironmentSpec(name="exact-numeric", dependencies={})

    class Inputs(IOModel):
        integer: int
        measurement: float

    class Outputs(IOModel):
        integer: int
        measurement: float

    def process_row(self, arguments):
        assert arguments.integer == 2**63 + 5
        assert arguments.measurement == 0.5
        return self.Outputs(
            integer=arguments.integer, measurement=arguments.measurement
        )


def test_declared_numeric_looking_string_survives_public_execution_and_cache(tmp_path):
    for _ in range(2):
        with Workflow(engine="direct", storage_path=tmp_path) as workflow:
            result = workflow.compute(ExactNumericSource()())
        assert result.at["row", "identifier"] == "001"
        assert isinstance(result.at["row", "identifier"], str)


def test_large_integer_and_float_survive_public_row_arguments_and_results(tmp_path):
    with Workflow(engine="direct", storage_path=tmp_path) as workflow:
        source = ExactNumericSource()()
        echoed = EchoExactNumeric()(
            integer=source["integer"], measurement=source["measurement"]
        )
        result = workflow.compute(echoed)
    assert int(result.at["row", "integer"]) == 2**63 + 5
    assert result.at["row", "measurement"] == 0.5


@pytest.mark.parametrize("dtype,base", [("uint64", 2**63), ("int64", 2**60)])
def test_dataframe_identity_distinguishes_exact_integer_cells_beside_float(dtype, base):
    def frame(integer):
        return pd.DataFrame(
            {"integer": np.array([integer], dtype=dtype), "measurement": [0.5]}
        )

    assert canonical_dataframe_digest(frame(base + 5)) != canonical_dataframe_digest(
        frame(base + 6)
    )


def test_environment_identity_preserves_channel_precedence():
    first = {"conda": {"channels": ["first", "second"], "dependencies": ["numpy=1.26"]}}
    reversed_channels = {
        "conda": {"channels": ["second", "first"], "dependencies": ["numpy=1.26"]}
    }
    assert compute_env_hash(first) != compute_env_hash(reversed_channels)
