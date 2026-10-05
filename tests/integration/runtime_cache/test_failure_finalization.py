"""Public run setup and DataFrame publication finalization contracts."""

import json
from pathlib import Path

import pytest

from bioimageflow import Workflow, WorkflowCancelledError, WorkflowExecutionContext
from bioimageflow.storage import CacheCorruptionError, Storage
from tests.testkit.runtime_cache import CountingTable


def _workflow(storage_path: Path, value: int) -> Workflow:
    workflow = Workflow(engine="direct", storage_path=storage_path)
    with workflow:
        table = CountingTable()(value=value)
    workflow.output("value", table["value"])
    return workflow


def _protected_success(storage_path: Path, context: WorkflowExecutionContext):
    latest = storage_path / "views/runs/latest-success.bioimageflow-link.json"
    assert context.terminal_status == "succeeded"
    outcome, = context.execution_outcomes
    assert outcome.result_key is not None and outcome.record_id is not None
    storage = Storage(storage_path)
    record = storage.result_dir(outcome.result_key) / "records" / outcome.record_id
    paths = [latest, storage.result_dir(outcome.result_key) / "current.json", *record.rglob("*")]
    return {path: path.read_bytes() for path in paths if path.is_file()}


def _run_json(storage_path: Path, context: WorkflowExecutionContext):
    return json.loads((storage_path / "views/runs" / context.run_id / "run.json").read_text())


def test_normal_root_run_finalizes_exact_context_and_selected_record(tmp_path):
    CountingTable.executions = 0
    workflow = _workflow(tmp_path, 4)
    context = WorkflowExecutionContext()
    engine = workflow.create_engine()
    try:
        frame = workflow.compute(engine=engine, run_context=context)
        assert frame.loc["row", "value"] == 4
        assert workflow.last_execution_context is context
        assert context.terminal_status == "succeeded"
        assert _run_json(tmp_path, context)["status"] == "succeeded"
        protected = _protected_success(tmp_path, context)
        assert protected
        assert CountingTable.executions == 1
        second = WorkflowExecutionContext()
        assert workflow.compute(engine=engine, run_context=second).loc["row", "value"] == 4
        assert second.terminal_status == "succeeded"
        assert CountingTable.executions == 1
    finally:
        engine.close()
        workflow.shared_memory_context.close()


@pytest.mark.parametrize("execution", ["compute", "steps"])
def test_setup_write_failure_finalizes_same_run_without_dispatch(tmp_path, monkeypatch, execution):
    CountingTable.executions = 0
    prior = _workflow(tmp_path, 4)
    prior_context = WorkflowExecutionContext()
    prior.compute(run_context=prior_context)
    protected = _protected_success(tmp_path, prior_context)
    sentinel = tmp_path / "foreign-sentinel"
    sentinel.write_bytes(b"foreign-owned")
    protected[sentinel] = sentinel.read_bytes()
    workflow = _workflow(tmp_path, 9)
    engine = workflow.create_engine()
    context = WorkflowExecutionContext()
    primary = OSError("injected running-view publication failure")
    original = Storage.write_run_metadata
    published = []

    def fail_after_running_write(storage, run_id, **kwargs):
        path = original(storage, run_id, **kwargs)
        if run_id == context.run_id and kwargs["status"] == "running":
            published.append(json.loads(path.read_text()))
            raise primary
        return path

    try:
        with monkeypatch.context() as patch:
            patch.setattr(Storage, "write_run_metadata", fail_after_running_write)
            with pytest.raises(OSError) as raised:
                if execution == "compute":
                    workflow.compute(engine=engine, run_context=context)
                else:
                    steps = workflow.compute_steps(engine=engine, run_context=context)
                    try:
                        next(steps)
                    finally:
                        steps.close()
        assert raised.value is primary
        assert len(published) == 1 and published[0]["run_id"] == context.run_id
        assert published[0]["status"] == "running"
        assert CountingTable.executions == 1
        assert workflow.last_execution_context is context
        assert not context.execution_outcomes
        assert all(path.read_bytes() == content for path, content in protected.items())
        failed_context_status = context.terminal_status
        run_path = tmp_path / "views/runs" / context.run_id / "run.json"
        failed_run_status = _run_json(tmp_path, context)["status"] if run_path.exists() else None
        fresh = WorkflowExecutionContext()
        assert workflow.compute(engine=engine, run_context=fresh).loc["row", "value"] == 9
        assert fresh.terminal_status == "succeeded"
        assert CountingTable.executions == 2
        assert failed_context_status == "failed"
        assert failed_run_status in {None, "failed"}
        assert context.terminal_status == failed_context_status
    finally:
        engine.close()
        workflow.shared_memory_context.close()
        prior.shared_memory_context.close()


@pytest.mark.parametrize("failure_type, expected_status, expected_error_type", [
    (RuntimeError, "failed", "RuntimeError"),
    (WorkflowCancelledError, "cancelled", None),
], ids=["failure", "cancellation-fault"])
def test_dataframe_selection_failure_terminalizes_exact_attempt(
    tmp_path, monkeypatch, failure_type, expected_status, expected_error_type,
):
    CountingTable.executions = 0
    prior = _workflow(tmp_path, 4)
    prior_context = WorkflowExecutionContext()
    prior.compute(run_context=prior_context)
    protected = _protected_success(tmp_path, prior_context)
    workflow = _workflow(tmp_path, 9)
    engine = workflow.create_engine()
    context = WorkflowExecutionContext()
    primary = failure_type("injected DataFrame selection failure")
    original = Storage.select_current_record
    observed = []

    def fail_selection(storage, result_key, **kwargs):
        if kwargs["run_id"] == context.run_id:
            attempt_path = storage.result_dir(result_key) / "attempts" / kwargs["attempt_id"] / "attempt.json"
            observed.append((result_key, kwargs["attempt_id"], attempt_path, json.loads(attempt_path.read_text())))
            raise primary
        return original(storage, result_key, **kwargs)

    try:
        with monkeypatch.context() as patch:
            patch.setattr(Storage, "select_current_record", fail_selection)
            with pytest.raises(failure_type) as raised:
                workflow.compute(engine=engine, run_context=context)
        assert raised.value is primary
        assert len(observed) == 1
        key, attempt_id, attempt_path, running = observed[0]
        assert running["run_id"] == context.run_id
        assert running["result_key"] == key and running["attempt_id"] == attempt_id
        assert running["status"] == "running"
        assert CountingTable.executions == 2
        assert context.terminal_status == "failed"
        assert _run_json(tmp_path, context)["status"] == expected_status
        assert not context.execution_outcomes
        assert not (Storage(tmp_path).result_dir(key) / "current.json").exists()
        assert all(path.read_bytes() == content for path, content in protected.items())
        terminal = json.loads(attempt_path.read_text())
        fresh = WorkflowExecutionContext()
        assert workflow.compute(engine=engine, run_context=fresh).loc["row", "value"] == 9
        assert fresh.terminal_status == "succeeded"
        assert terminal["status"] == expected_status
        assert terminal["error_type"] == expected_error_type
        assert terminal["completed_at"] is not None
    finally:
        engine.close()
        workflow.shared_memory_context.close()
        prior.shared_memory_context.close()


def test_setup_failure_before_publication_releases_exact_context(tmp_path, monkeypatch):
    workflow = _workflow(tmp_path, 4)
    engine = workflow.create_engine()
    context = WorkflowExecutionContext()
    primary = OSError("injected pre-publication setup failure")
    CountingTable.executions = 0

    def fail_start(storage, run_id, **kwargs):
        assert run_id == context.run_id
        raise primary

    try:
        with monkeypatch.context() as patch:
            patch.setattr(Storage, "start_run_metadata", fail_start)
            with pytest.raises(OSError) as raised:
                workflow.compute(engine=engine, run_context=context)
        assert raised.value is primary
        assert CountingTable.executions == 0
        assert not context.execution_outcomes
        assert not (tmp_path / "views/runs" / context.run_id).exists()
        failed_context_status = context.terminal_status
        fresh = WorkflowExecutionContext()
        assert workflow.compute(engine=engine, run_context=fresh).loc["row", "value"] == 4
        assert fresh.terminal_status == "succeeded"
        assert failed_context_status == "failed"
        assert context.terminal_status == failed_context_status
    finally:
        engine.close()
        workflow.shared_memory_context.close()


def test_setup_refuses_preexisting_run_without_mutating_its_view(tmp_path):
    prior = _workflow(tmp_path, 4)
    prior_context = WorkflowExecutionContext()
    CountingTable.executions = 0
    prior.compute(run_context=prior_context)
    protected = _protected_success(tmp_path, prior_context)
    run_dir = tmp_path / "views/runs" / prior_context.run_id
    protected.update({path: path.read_bytes() for path in run_dir.rglob("*") if path.is_file()})
    workflow = _workflow(tmp_path, 9)
    engine = workflow.create_engine()
    context = WorkflowExecutionContext(prior_context.run_id)
    try:
        with pytest.raises(CacheCorruptionError, match="canonical view"):
            workflow.compute(engine=engine, run_context=context)
        assert CountingTable.executions == 1
        assert not context.execution_outcomes
        assert all(path.read_bytes() == content for path, content in protected.items())
        failed_context_status = context.terminal_status
        fresh = WorkflowExecutionContext()
        assert workflow.compute(engine=engine, run_context=fresh).loc["row", "value"] == 9
        assert fresh.terminal_status == "succeeded"
        assert failed_context_status == "failed"
        assert context.terminal_status == failed_context_status
    finally:
        engine.close()
        workflow.shared_memory_context.close()
        prior.shared_memory_context.close()


def test_completed_deferred_run_persistence_failure_keeps_retry_authority(tmp_path, monkeypatch):
    workflow = _workflow(tmp_path, 4)
    context = WorkflowExecutionContext(defer_success_finalization=True)
    engine = workflow.create_engine()
    primary = OSError("injected final metadata persistence failure")

    def fail_finalize(storage, run_id, **kwargs):
        assert run_id == context.run_id
        raise primary

    try:
        assert workflow.compute(engine=engine, run_context=context).loc["row", "value"] == 4
        assert context.execution_outcomes
        with monkeypatch.context() as patch:
            patch.setattr(Storage, "finalize_run_metadata", fail_finalize)
            with pytest.raises(OSError) as raised:
                context.finalize_success()
        assert raised.value is primary
        assert context.terminal_status is None
        assert _run_json(tmp_path, context)["status"] == "running"
        context.finalize_success()
        assert context.terminal_status == "succeeded"
        assert _run_json(tmp_path, context)["status"] == "succeeded"
    finally:
        engine.close()
        workflow.shared_memory_context.close()


def test_primary_error_survives_pending_attempt_and_run_finalization(tmp_path, monkeypatch):
    prior = _workflow(tmp_path, 4)
    prior_context = WorkflowExecutionContext()
    prior.compute(run_context=prior_context)
    protected = _protected_success(tmp_path, prior_context)
    workflow = _workflow(tmp_path, 9)
    context = WorkflowExecutionContext()
    engine = workflow.create_engine()
    primary = RuntimeError("injected selection primary")
    attempt_error = OSError("injected attempt terminal persistence failure")
    run_error = OSError("injected run terminal persistence failure")
    observed = []
    fault_active = [True]
    original_select = Storage.select_current_record
    original_attempt = Storage.finish_cache_attempt
    original_run = Storage.finalize_run_metadata

    def reject_selection(storage, result_key, **kwargs):
        if kwargs["run_id"] == context.run_id:
            observed.append((result_key, kwargs["attempt_id"]))
            raise primary
        return original_select(storage, result_key, **kwargs)

    def fail_attempt(storage, result_key, attempt_id, **kwargs):
        if fault_active[0] and (result_key, attempt_id) in observed and kwargs["status"] == "failed":
            raise attempt_error
        return original_attempt(storage, result_key, attempt_id, **kwargs)

    def fail_run(storage, run_id, **kwargs):
        if fault_active[0] and run_id == context.run_id and kwargs["status"] == "failed":
            raise run_error
        return original_run(storage, run_id, **kwargs)

    try:
        with monkeypatch.context() as patch:
            patch.setattr(Storage, "select_current_record", reject_selection)
            patch.setattr(Storage, "finish_cache_attempt", fail_attempt)
            patch.setattr(Storage, "finalize_run_metadata", fail_run)
            with pytest.raises(RuntimeError) as raised:
                workflow.compute(engine=engine, run_context=context)
        assert raised.value is primary
        assert context.terminal_status == "failed"
        assert not context.execution_outcomes
        assert context.cleanup_pending
        diagnostics = context.cleanup_errors
        assert isinstance(diagnostics, tuple)
        assert [(item.phase, item.error_type, item.message) for item in diagnostics] == [
            ("cache-attempt-finalization", "OSError", str(attempt_error)),
            ("run-finalization", "OSError", str(run_error)),
        ]
        key, attempt_id = observed[0]
        attempt_path = Storage(tmp_path).result_dir(key) / "attempts" / attempt_id / "attempt.json"
        assert json.loads(attempt_path.read_text())["status"] == "running"
        assert _run_json(tmp_path, context)["status"] == "running"
        assert all(path.read_bytes() == content for path, content in protected.items())
        fault_active[0] = False
        context.retry_cleanup()
        assert not context.cleanup_pending
        assert context.terminal_status == "failed"
        assert context.cleanup_errors == diagnostics
        terminal = json.loads(attempt_path.read_text())
        assert terminal["status"] == "failed" and terminal["error_type"] == "RuntimeError"
        assert _run_json(tmp_path, context)["status"] == "failed"
        assert all(path.read_bytes() == content for path, content in protected.items())
    finally:
        engine.close()
        workflow.shared_memory_context.close()
        prior.shared_memory_context.close()
