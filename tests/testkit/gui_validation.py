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


def _graph(
    tmp_path: Path,
    *,
    nodes: list[dict[str, Any]] | None = None,
    edges: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "name": "gui-test",
        "display_name": "GUI Test",
        "interface": {"inputs": [], "outputs": []},
        "nodes": nodes or [],
        "edges": edges or [],
        "config": {
            "storage_path": str(tmp_path),
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

    display_name = "BadConstraint"
    environment = EnvironmentSpec(
        name="_validateenv",
        dependencies={"conda": ["numpy=2.4.2"], "python": "3.12"},
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
