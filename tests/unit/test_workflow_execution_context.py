"""Execution-context and active-execution contract tests."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pandas as pd
import pytest

from bioimageflow import (
    DataFrameTool,
    DefaultEngine,
    Workflow,
    WorkflowCancelledError,
    WorkflowExecutionContext,
)
from bioimageflow_core import IOModel
from tests.testkit.runtime_cache import CountingTable


class _DeclaredTable(DataFrameTool):
    accepts_upstream = False

    class Inputs(IOModel):
        value: int

    class Outputs(IOModel):
        value: int

    def transform(self, df, arguments):
        return pd.DataFrame({"value": [arguments.value]}, index=["row"])


def test_context_validates_run_id_and_deferred_flag() -> None:
    context = WorkflowExecutionContext(
        "run_0123456789abcdef0123456789abcdef",
        defer_success_finalization=True,
    )

    assert context.run_id == "run_0123456789abcdef0123456789abcdef"
    assert context.defer_success_finalization is True

    with pytest.raises(ValueError, match="run_id"):
        WorkflowExecutionContext("unsafe")
    with pytest.raises(TypeError, match="bool"):
        WorkflowExecutionContext(defer_success_finalization=1)  # type: ignore[arg-type]


def test_idle_cancel_does_not_affect_next_execution(tmp_path) -> None:
    workflow = Workflow(engine="direct", storage_path=tmp_path)
    workflow.cancel()

    with workflow:
        node = CountingTable()(value=3)

    result = workflow.compute(node)

    assert result.loc["row", "value"] == 3
    assert workflow.cancel_requested is False


def test_pre_requested_context_cancels_at_start(tmp_path) -> None:
    context = WorkflowExecutionContext()
    context.request_cancel()
    workflow = Workflow(engine="direct", storage_path=tmp_path)
    with workflow:
        node = CountingTable()(value=4)

    with pytest.raises(WorkflowCancelledError):
        workflow.compute(node, run_context=context)

    assert context.terminal_status == "failed"
    run = json.loads(
        next((tmp_path / "views" / "runs").glob("run_*/run.json")).read_text()
    )
    assert run["status"] == "cancelled"


def test_deferred_success_requires_explicit_finalization(tmp_path) -> None:
    context = WorkflowExecutionContext(defer_success_finalization=True)
    workflow = Workflow(engine="direct", storage_path=tmp_path)
    with workflow:
        node = CountingTable()(value=5)

    workflow.compute(node, run_context=context)

    run_path = next((tmp_path / "views" / "runs").glob("run_*/run.json"))
    assert json.loads(run_path.read_text())["status"] == "running"
    assert context.terminal_status is None

    context.finalize_success()

    assert context.terminal_status == "succeeded"
    assert json.loads(run_path.read_text())["status"] == "succeeded"
    with pytest.raises(RuntimeError, match="only after"):
        context.finalize_success()


def test_steps_reserve_workflow_before_first_iteration(tmp_path) -> None:
    workflow = Workflow(engine="direct", storage_path=tmp_path)
    with workflow:
        node = CountingTable()(value=6)

    steps = workflow.compute_steps(node)

    with pytest.raises(RuntimeError, match="active execution"):
        workflow.compute(node)
    steps.close()

    result = workflow.compute(node)
    assert result.loc["row", "value"] == 6


def test_engine_steps_reserve_engine_before_first_iteration() -> None:
    engine = DefaultEngine()
    workflow = SimpleNamespace(cancel_requested=False)

    steps = engine.execute_steps([], workflow)

    with pytest.raises(RuntimeError, match="active execution"):
        engine.execute([], workflow)
    steps.close()

    assert engine._execution_active is False


def test_root_input_wrapper_uses_exact_context(tmp_path) -> None:
    workflow = Workflow(engine="direct", storage_path=tmp_path)
    workflow.input("value", int)
    with workflow:
        node = _DeclaredTable()(
            value=workflow._input_ref(next(iter(workflow._interface_inputs)))
        )
    workflow.output("result", node["value"])

    class TrackingEngine(DefaultEngine):
        seen_context: WorkflowExecutionContext | None = None

        def execute(self, targets, executed_workflow):
            self.seen_context = executed_workflow._active_run_context
            return super().execute(targets, executed_workflow)

    context = WorkflowExecutionContext()
    engine = TrackingEngine()

    workflow.compute(inputs={"value": 7}, engine=engine, run_context=context)

    assert engine.seen_context is context
