from __future__ import annotations

from importlib.metadata import version

from bioimageflow import Workflow
from bioimageflow_core import EnvironmentSpec, IOModel, ProcessingTool, RowConsumption


def _tool_with_environment(environment: EnvironmentSpec) -> type[ProcessingTool]:
    declared_environment = environment

    class CoreTool(ProcessingTool):
        row_consumption = RowConsumption.MAPPED
        environment = declared_environment

        class Inputs(IOModel):
            pass

        class Outputs(IOModel):
            value: int

        def process_row(self, arguments, *, context=None):
            return self.Outputs(value=1)

    return CoreTool


def test_workflow_validation_rejects_incompatible_explicit_core(tmp_path) -> None:
    tool = _tool_with_environment(
        EnvironmentSpec(
            name="incompatible-core",
            dependencies={"pip": ["bioimageflow-core==0"]},
        )
    )
    with Workflow(storage_path=tmp_path) as workflow:
        tool()(name="worker")

    errors = workflow.validate()

    assert [(error.kind, error.node) for error in errors] == [
        ("environment_incompatible", "worker")
    ]
    assert f"active bioimageflow-core=={version('bioimageflow-core')}" in errors[0].message


def test_workflow_validation_accepts_compatible_explicit_core(tmp_path) -> None:
    installed = version("bioimageflow-core")
    tool = _tool_with_environment(
        EnvironmentSpec(
            name="compatible-core",
            dependencies={"pip": [f"bioimageflow-core=={installed}"]},
        )
    )
    with Workflow(storage_path=tmp_path) as workflow:
        tool()(name="worker")

    assert workflow.validate() == []
