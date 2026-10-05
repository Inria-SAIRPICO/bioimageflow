"""Public observer errors cannot decide a scientific result."""
import pytest
from bioimageflow import Workflow, WorkflowExecutionContext
from bioimageflow.storage import Storage
from bioimageflow_core import GENERAL_ENV, IOModel, ProcessingTool, RowConsumption

SCIENCE_ERROR = ValueError("original science failure")
OBSERVER_ERROR = RuntimeError("observer failure")

class ObservedTool(ProcessingTool):
    environment = GENERAL_ENV
    row_consumption = RowConsumption.MAPPED
    executions = 0
    class Inputs(IOModel):
        fail: bool
    class Outputs(IOModel):
        value: int
    def process_row(self, arguments):
        type(self).executions += 1
        if arguments.fail:
            raise SCIENCE_ERROR
        return self.Outputs(value=4)


def test_progress_started_observer_does_not_abort_science(tmp_path):
    ObservedTool.executions = 0
    def observer(event):
        if event.status == "started":
            raise OBSERVER_ERROR
    context = WorkflowExecutionContext()
    with Workflow(engine="direct", storage_path=tmp_path, on_progress=observer) as workflow:
        node = ObservedTool()(fail=False)
    result = workflow.compute(node, run_context=context)
    assert result["value"].tolist() == [4]
    assert ObservedTool.executions == 1
    assert context.terminal_status == "succeeded"


def test_progress_failed_observer_preserves_scientific_primary(tmp_path):
    ObservedTool.executions = 0
    def observer(event):
        if event.status == "failed":
            raise OBSERVER_ERROR
    context = WorkflowExecutionContext()
    with Workflow(engine="direct", storage_path=tmp_path, on_progress=observer) as workflow:
        node = ObservedTool()(fail=True)
    with pytest.raises(ValueError) as caught:
        workflow.compute(node, run_context=context)
    assert caught.value is SCIENCE_ERROR
    assert ObservedTool.executions == 1
    assert context.terminal_status == "failed"


def test_progress_observer_can_cancel_and_read_status_reentrantly(tmp_path):
    from bioimageflow.engine import WorkflowCancelledError

    context = WorkflowExecutionContext()
    seen = []
    def observer(event):
        if event.status == "started":
            seen.append(context.terminal_status)
            context.request_cancel()
            seen.append(context.cancel_requested)
    with Workflow(engine="direct", storage_path=tmp_path, on_progress=observer) as workflow:
        node = ObservedTool()(fail=False)
    with pytest.raises(WorkflowCancelledError):
        workflow.compute(node, run_context=context)
    assert seen == [None, True]
    assert context.terminal_status == "failed"
    assert not context.cleanup_pending


def test_processing_terminal_write_failure_preserves_scientific_primary(tmp_path, monkeypatch):
    secondary = OSError("failed Processing attempt terminal persistence")
    original = Storage.finish_cache_attempt
    def fail_terminal(storage, result_key, attempt_id, **kwargs):
        if kwargs["status"] == "failed":
            raise secondary
        return original(storage, result_key, attempt_id, **kwargs)
    context = WorkflowExecutionContext()
    with Workflow(engine="direct", storage_path=tmp_path) as workflow:
        node = ObservedTool()(fail=True)
    monkeypatch.setattr(Storage, "finish_cache_attempt", fail_terminal)
    with pytest.raises(ValueError) as caught:
        workflow.compute(node, run_context=context)
    assert caught.value is SCIENCE_ERROR
    assert context.terminal_status == "failed"
    assert context.cleanup_pending
    assert context.cleanup_errors[0].phase == "cache-attempt-finalization"
    monkeypatch.setattr(Storage, "finish_cache_attempt", original)
    context.retry_cleanup()
    assert not context.cleanup_pending


def test_unprintable_observer_failure_is_a_detached_diagnostic(tmp_path):
    class UnprintableError(Exception):
        def __str__(self):
            raise RuntimeError("broken diagnostic string")
    context = WorkflowExecutionContext()
    def observer(event):
        if event.status == "started":
            raise UnprintableError()
    with Workflow(engine="direct", storage_path=tmp_path, on_progress=observer) as workflow:
        node = ObservedTool()(fail=False)
    assert workflow.compute(node, run_context=context)["value"].tolist() == [4]
    assert context.terminal_status == "succeeded"
    assert context.cleanup_errors[0].message == "<unprintable exception>"
