"""Focused Wetlands dispatch cancellation synchronization tests."""

from types import SimpleNamespace

from bioimageflow import WorkflowExecutionContext
from bioimageflow.engine.dispatch import _WetlandsTaskTracker


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
