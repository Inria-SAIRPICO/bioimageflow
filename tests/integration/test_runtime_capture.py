"""Public execution admission owns effective values and environment settings."""

from __future__ import annotations

import pandas as pd
import pytest

from bioimageflow import DataFrameTool, DefaultEngine, Workflow
from bioimageflow_core import EnvironmentSpec, IOModel, ProcessingTool, RowConsumption


class _CaptureSource(ProcessingTool):
    row_consumption = RowConsumption.MAPPED
    environment = EnvironmentSpec(name="capture-source", dependencies={})

    class Inputs(IOModel):
        value: int = 4

    class Outputs(IOModel):
        value: int

    def process_row(self, arguments):
        return self.Outputs(value=arguments.value)


@pytest.mark.parametrize("root", [False, True], ids=["explicit", "root"])
@pytest.mark.parametrize("steps", [False, True], ids=["compute", "steps"])
def test_public_execution_captures_defaults_and_environment_before_effects(
    tmp_path, monkeypatch, root, steps
):
    workflow = Workflow(engine="direct", storage_path=tmp_path)
    with workflow:
        source = _CaptureSource()()
    workflow.output("value", source["value"])
    configuration = workflow.get_environment(_CaptureSource.environment)
    configuration.max_workers = 2
    observed_configurations = []

    def edit_original():
        monkeypatch.setattr(_CaptureSource.Inputs, "value", 99)
        configuration.max_workers = 9

    class HoldingEngine(DefaultEngine):
        def observe(self, executed_workflow):
            configuration = executed_workflow._env_configs.get("capture-source")
            observed_configurations.append(
                None if configuration is None else configuration.max_workers
            )

        def execute(self, targets, executed_workflow):
            edit_original()
            self.observe(executed_workflow)
            return super().execute(targets, executed_workflow)

        def execute_steps(self, targets, executed_workflow):
            self.observe(executed_workflow)
            yield from super().execute_steps(targets, executed_workflow)

    engine = HoldingEngine()
    targets = () if root else (source,)
    if steps:
        iterator = workflow.compute_steps(*targets, engine=engine)
        edit_original()
        executed = [step.execute() for step in iterator]
        result = executed[-1]
    else:
        result = workflow.compute(*targets, engine=engine)

    assert result["value"].tolist() == [4]
    assert observed_configurations == [2]
    assert configuration.max_workers == 9
    assert _CaptureSource.Inputs.value == 99
    assert workflow._active_run_context is None

    # A new admission sees accepted edits; freezing cannot become a response cache.
    result = workflow.compute(*targets, engine=DefaultEngine())
    assert result["value"].tolist() == [99]


def test_steps_capture_root_dataframe_before_iteration(tmp_path):
    class ReadTable(DataFrameTool):
        class Outputs(IOModel):
            value: int

        def transform(self, frame, arguments):
            return frame.copy()

    workflow = Workflow(engine="direct", storage_path=tmp_path)
    incoming = workflow.input("table", kind="dataframe")
    with workflow:
        result_node = ReadTable()(incoming)
    workflow.output("value", result_node["value"])
    frame = pd.DataFrame({"value": [4]}, index=["sample"])

    steps = workflow.compute_steps(inputs={"table": frame})
    frame.at["sample", "value"] = 99
    result = [step.execute() for step in steps][-1]

    assert result["value"].tolist() == [4]
    assert result.index.tolist() == ["sample"]
    assert frame.at["sample", "value"] == 99


def test_steps_capture_supported_mutable_root_value(tmp_path):
    class SumValues(DataFrameTool):
        accepts_upstream = False

        class Inputs(IOModel):
            values: list[int]

        class Outputs(IOModel):
            value: int

        def transform(self, frame, arguments):
            return pd.DataFrame({"value": [sum(arguments.values)]})

    workflow = Workflow(engine="direct", storage_path=tmp_path)
    incoming = workflow.input("values", list[int])
    with workflow:
        result_node = SumValues()(values=incoming)
    workflow.output("value", result_node["value"])
    values = [4]

    steps = workflow.compute_steps(inputs={"values": values})
    values[0] = 99
    result = [step.execute() for step in steps][-1]

    assert result["value"].tolist() == [4]
    assert values == [99]
