"""Focused tests split from ``tests/integration/test_gui_validation_api.py``."""

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


from tests.testkit.gui_validation import (
    _BadConstraintTool,
)


class TestValidate:
    def test_empty_workflow(self, tmp_path) -> None:
        wf = Workflow(storage_path=tmp_path, engine="direct")
        assert wf.validate() == []

    def test_valid_workflow(self, tmp_path: Path) -> None:
        wf = Workflow(engine="direct", storage_path=tmp_path)
        with wf:
            load = FileLoader()(path=str(tmp_path))
            seg = StubSegmenter()(input_image=load["path"])
            StubStats()(image=load["path"], mask=seg["mask"])
        assert wf.validate() == []

    def test_missing_required_after_capture(self, tmp_path) -> None:
        wf = Workflow(storage_path=tmp_path, engine="direct")
        with wf:
            with wf.capture_errors():
                StubSegmenter()()
        errs = wf.validate()
        assert any(e.kind == "missing_input" and e.field == "input_image" for e in errs)

    def test_parameter_invalid_via_pydantic_constraint(self, tmp_path) -> None:
        wf = Workflow(storage_path=tmp_path, engine="direct")
        with wf:
            _BadConstraintTool()(diameter=-1)  # gt=0 violated
        errs = wf.validate()
        assert any(
            e.kind == "parameter_invalid" and e.field == "diameter" for e in errs
        )

    def test_validate_parameters_standalone(self) -> None:
        errs = validate_parameters(_BadConstraintTool, {"diameter": -5})
        assert errs
        assert errs[0].kind == "parameter_invalid"

    def test_validate_parameters_empty(self) -> None:
        errs = validate_parameters(_BadConstraintTool, {})
        assert errs == []

    def test_topological_order_raises_on_cycle(self, tmp_path) -> None:
        # A cycle is only constructible by mutating upstream_nodes after the fact.
        wf = Workflow(storage_path=tmp_path, engine="direct")
        with wf:
            load = FileLoader()(path="/tmp/x")
            seg = StubSegmenter()(input_image=load["path"])
        # Force a cycle.
        seg._upstream_nodes.add(seg)
        from graphlib import CycleError

        with pytest.raises(CycleError):
            topological_order(wf)
        errs = wf.validate()
        assert any(e.kind == "cycle" for e in errs)

    def test_plan_raises_cycle_in_workflow_error(self, tmp_path) -> None:
        from bioimageflow import CycleInWorkflowError

        wf = Workflow(storage_path=tmp_path, engine="direct")
        with wf:
            load = FileLoader()(path="/tmp/x")
            seg = StubSegmenter()(input_image=load["path"])
        seg._upstream_nodes.add(seg)
        with pytest.raises(CycleInWorkflowError) as excinfo:
            wf.plan()
        # Carries the offending node names; subclass of ValueError.
        assert isinstance(excinfo.value, ValueError)
        assert excinfo.value.nodes  # non-empty
        # validate() still reports cycles non-fatally (unchanged behavior).
        errs = wf.validate()
        assert any(e.kind == "cycle" for e in errs)
