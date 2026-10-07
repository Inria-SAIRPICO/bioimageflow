"""Shared helpers for the focused tests split from ``tests/integration/test_gui_validation_api.py``."""

# ruff: noqa: F401

import json

from pathlib import Path

from typing import Annotated, Any

import pytest

from pydantic import Field

from bioimageflow_core import (
    Arguments,
    EnvironmentSpec,
    IOModel,
    ImageSpec,
    ProcessingTool,
    RowConsumption,
    Semantic,
    Layout,
    Template,
)

from bioimageflow import (
    NodePlan,
    SourceToolUpstreamError,
    ValidationError,
    Workflow,
    get_inputs_schema,
    serialize_image_spec,
    serialize_resolved_outputs,
    serialize_tool_metadata,
    topological_order,
    validate_parameters,
)

from bioimageflow.node import BindingError, ColumnNotFoundError, IndexAlignmentError

from tests.testkit.integration_tools import (
    FileLoader,
    StubSegmenter,
    StubStats,
)


def ordinary_planning_tool(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> type[ProcessingTool]:
    """Load selected tool bytes without pytest rewriting the owning package."""
    import importlib.util
    import sys

    source = tmp_path / "planning_effect_tool.py"
    source.write_text(
        "from pathlib import Path\n"
        "from bioimageflow_core import ProcessingTool, IOModel, EnvironmentSpec, RowConsumption\n"
        "class PlanningTool(ProcessingTool):\n"
        "    row_consumption = RowConsumption.MAPPED\n"
        "    environment = EnvironmentSpec('planning-only', {'python': '>=3.9'})\n"
        "    class Inputs(IOModel): input_image: Path\n"
        "    class Outputs(IOModel): value: int\n"
        "    def process_row(self, arguments):\n"
        "        raise RuntimeError('planning executed science')\n"
    )
    spec = importlib.util.spec_from_file_location("planning_effect_tool", source)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    return module.PlanningTool


def _graph(
    *,
    nodes: list[dict[str, Any]] | None = None,
    edges: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": 2,
        "name": "gui-test",
        "display_name": "GUI Test",
        "interface": {"inputs": [], "outputs": []},
        "nodes": nodes or [],
        "edges": edges or [],
        "config": {
            "engine": "direct",
            "execution": "parallel",
        },
    }


def _tool_node(
    name: str, module: str, class_name: str, *, constants: dict[str, Any] | None = None
) -> dict[str, Any]:
    return {
        "name": name,
        "type": "tool",
        "tool_module": module,
        "tool_class": class_name,
        "tool_package": None,
        "tool_package_version": None,
        "constants": constants or {},
    }


class _BadConstraintTool(ProcessingTool):
    """Inputs has a gt=0 constraint that can surface as parameter_invalid."""

    row_consumption = RowConsumption.MAPPED
    display_name = "BadConstraint"
    environment = EnvironmentSpec(
        name="_validateenv",
        dependencies={"conda": ["numpy==2.4.2"], "python": "3.12"},
    )

    class Inputs(IOModel):
        diameter: Annotated[float, Field(gt=0)] = 1.0

    class Outputs(IOModel):
        result: Path = Template("{diameter}.txt")

    def process_row(
        self, arguments: Arguments, *, context: object | None = None
    ) -> Any:
        p = Path(arguments.result)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x")
        return self.Outputs(result=p)
