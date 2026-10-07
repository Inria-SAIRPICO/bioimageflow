"""Focused Wetlands dispatch cancellation synchronization tests."""

from types import SimpleNamespace
from typing import Any
import importlib.util
import sys
import uuid

import pytest
from wetlands import ExecutionState

from bioimageflow import Workflow, WorkflowExecutionContext
from bioimageflow.engine import DefaultEngine, WorkflowCancelledError
from bioimageflow.engine.dispatch import _WetlandsTaskTracker
from bioimageflow_core import ResourceSpec
from tests.testkit.worker_timeout import _execution_contexts


class _TaskState:
    terminal = False


class _SubmittedTask:
    def __init__(self) -> None:
        self.state = _TaskState()
        self.cancel_calls = 0
        self.wait_calls = 0
        self._cancel_requested = False

    def cancel(self) -> bool:
        if self._cancel_requested:
            return False
        self._cancel_requested = True
        self.cancel_calls += 1
        return True

    def wait_for(self) -> None:
        self.wait_calls += 1
        self.state.terminal = True
        raise RuntimeError("cooperative cancellation")


def test_task_returned_after_cancellation_is_cancelled_and_drained() -> None:
    context = WorkflowExecutionContext()
    workflow = SimpleNamespace(_active_run_context=context)
    tracker = _WetlandsTaskTracker(workflow)
    task = _SubmittedTask()

    context.request_cancel()
    tracker.register([task])
    tracker.cancel_and_drain()
    tracker.close()

    assert task.cancel_calls == 1
    assert task.wait_calls == 1
    assert task.state.terminal


def test_closed_tracker_does_not_retain_or_cancel_tasks() -> None:
    context = WorkflowExecutionContext()
    workflow = SimpleNamespace(_active_run_context=context)
    tracker = _WetlandsTaskTracker(workflow)
    task = _SubmittedTask()
    tracker.register([task])

    tracker.close()
    context.request_cancel()

    assert task.cancel_calls == 0


class _DispatchTask:
    def __init__(self, *, interrupt_cleanup_once: bool = False) -> None:
        self.state = ExecutionState.RUNNING
        self.cancel_calls = 0
        self.wait_calls = 0
        self.interrupt_cleanup_once = interrupt_cleanup_once

    def cancel(self) -> bool:
        self.cancel_calls += 1
        return True

    def wait_for(self, timeout: float | None = None) -> None:
        del timeout
        self.wait_calls += 1
        if self.interrupt_cleanup_once:
            self.interrupt_cleanup_once = False
            raise KeyboardInterrupt("cleanup interrupted")
        self.state = ExecutionState.CANCELED
        raise RuntimeError("cancelled")

    def listen(self, callback: Any) -> None:
        del callback


class _PartialSubmissionManager:
    def __init__(
        self,
        context: WorkflowExecutionContext,
        *,
        cancel_during_first: bool = False,
        fail_second: bool = False,
        interrupt_cleanup_once: bool = False,
    ) -> None:
        self.context = context
        self.cancel_during_first = cancel_during_first
        self.fail_second = fail_second
        self.interrupt_cleanup_once = interrupt_cleanup_once
        self.submit_calls = 0
        self.tasks: list[_DispatchTask] = []
        self.shared_memory_grants: list[Any] = []
        self.submission_error = ValueError("second submission failed")

    def submit_processing_task(self, *args: Any, **kwargs: Any) -> _DispatchTask:
        del args
        self.shared_memory_grants.append(kwargs["shared_memory_grant"])
        self.submit_calls += 1
        if self.fail_second and self.submit_calls == 2:
            raise self.submission_error
        task = _DispatchTask(
            interrupt_cleanup_once=self.interrupt_cleanup_once
            and self.submit_calls == 1
        )
        self.tasks.append(task)
        if self.cancel_during_first and self.submit_calls == 1:
            self.context.request_cancel()
        return task

    def map_processing_tasks(self, *args: Any, **kwargs: Any) -> list[_DispatchTask]:
        """Model the old all-at-once adapter path that loses a partial result."""
        del kwargs
        return [self.submit_processing_task(args[0], payload) for payload in args[1]]


def _dispatch_rows(
    tmp_path: Any,
    manager: _PartialSubmissionManager,
    context: WorkflowExecutionContext,
    *,
    max_concurrent: int | None = None,
) -> None:
    module_name = "cancellation_tool_" + uuid.uuid4().hex
    source = tmp_path / (module_name + ".py")
    source.write_text(
        "from bioimageflow_core import ProcessingTool, IOModel, EnvironmentSpec, RowConsumption\n"
        "class StubTool(ProcessingTool):\n"
        "    row_consumption = RowConsumption.MAPPED\n"
        "    environment = EnvironmentSpec('stub_wt_env', {})\n"
        "    class Inputs(IOModel): a: int\n"
        "    class Outputs(IOModel): value: float = 0.0\n"
        "    def process_row(self, arguments, *, context=None): return self.Outputs()\n"
    )
    spec = importlib.util.spec_from_file_location(module_name, source)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    engine = DefaultEngine(use_wetlands=True, env_manager=manager,  # type: ignore[arg-type]
                           resource_lifetime="external")
    workflow = Workflow(storage_path=tmp_path, engine="direct")
    with workflow:
        node = module.StubTool()(a=1, name="cancel_node")
    reachable, dependencies, _ = engine._compile_execution_graph([node])
    engine._compiled_ordinals = {
        current: ordinal for ordinal, current in enumerate(engine._topological_sort(reachable, dependencies))
    }
    engine._capture_executable(node)
    workflow._active_run_context = context
    owner = workflow.shared_memory_context
    row_contexts, batch_context = _execution_contexts(3)
    try:
        engine._dispatch_via_wetlands(
            node.tool,
            arguments_dicts=[{"a": 1}, {"a": 2}, {"a": 3}],
            workflow=workflow,
            node_name=node.name,
            has_batch=False,
            row_contexts=row_contexts,
            batch_context=batch_context,
            invocation_id=f"inv_{'1' * 32}",
            cache_attempt_id=f"att_{'2' * 32}",
            resources=ResourceSpec(max_concurrent=max_concurrent),
        )
    finally:
        # These fake tasks have no physical worker; settle only their received
        # grants after dispatch, without changing task cancel/wait/state effects.
        for grant in manager.shared_memory_grants:
            grant.drained()
        assert owner.close().state == "closed"
        engine.close()
        if sys.modules.get(module_name) is module:
            sys.modules.pop(module_name)


def test_partial_submission_failure_drains_prior_task_despite_cleanup_interrupt(
    tmp_path: Any,
) -> None:
    context = WorkflowExecutionContext()
    manager = _PartialSubmissionManager(
        context,
        fail_second=True,
        interrupt_cleanup_once=True,
    )

    with pytest.raises(ValueError, match="second submission failed") as observed:
        _dispatch_rows(tmp_path, manager, context)

    assert observed.value is manager.submission_error
    assert manager.submit_calls == 2
    assert len(manager.tasks) == 1
    assert manager.tasks[0].cancel_calls == 1
    assert manager.tasks[0].wait_calls == 2
    assert manager.tasks[0].state == ExecutionState.CANCELED


def test_cancel_during_submission_cancels_returned_task_and_stops_later_work(
    tmp_path: Any,
) -> None:
    context = WorkflowExecutionContext()
    manager = _PartialSubmissionManager(context, cancel_during_first=True)

    with pytest.raises(WorkflowCancelledError):
        _dispatch_rows(tmp_path, manager, context, max_concurrent=1)

    assert manager.submit_calls == 1
    assert manager.tasks[0].cancel_calls >= 1
    assert manager.tasks[0].wait_calls == 1
    assert manager.tasks[0].state == ExecutionState.CANCELED


class _CoincidentOutcomeTask(_DispatchTask):
    def __init__(self, context: WorkflowExecutionContext, outcome: str) -> None:
        super().__init__()
        self.context = context
        self.outcome = outcome
        self.result: dict[str, object] = {}
        self.exception = RuntimeError("worker failed")

    def cancel(self) -> bool:
        if self.state.terminal:
            return False
        self.cancel_calls += 1
        self.state = ExecutionState.CANCELED
        return True

    def wait_for(self, timeout: float | None = None) -> None:
        del timeout
        self.wait_calls += 1
        if self.outcome == "completed":
            self.state = ExecutionState.COMPLETED
            self.context.request_cancel()
            return
        if self.outcome == "failed":
            self.state = ExecutionState.FAILED
            self.context.request_cancel()
            raise self.exception
        self.context.request_cancel()
        raise TimeoutError("engine timeout")


class _CoincidentOutcomeManager(_PartialSubmissionManager):
    def __init__(self, context: WorkflowExecutionContext, outcome: str) -> None:
        super().__init__(context)
        self.outcome = outcome

    def submit_processing_task(
        self,
        *args: Any,
        **kwargs: Any,
    ) -> _CoincidentOutcomeTask:
        del args
        self.shared_memory_grants.append(kwargs["shared_memory_grant"])
        self.submit_calls += 1
        task = _CoincidentOutcomeTask(self.context, self.outcome)
        self.tasks.append(task)
        return task


@pytest.mark.parametrize("outcome", ["completed", "failed", "timeout"])
def test_cancellation_wins_before_coincident_task_outcome_is_accepted(
    tmp_path: Any,
    outcome: str,
) -> None:
    context = WorkflowExecutionContext()
    manager = _CoincidentOutcomeManager(context, outcome)

    with pytest.raises(WorkflowCancelledError):
        _dispatch_rows(tmp_path, manager, context, max_concurrent=1)

    assert context.cancel_requested
    assert manager.submit_calls == 1
    assert manager.tasks[0].wait_calls == 1
