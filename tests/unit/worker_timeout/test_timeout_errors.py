"""Focused tests split from ``tests/unit/test_worker_timeout.py``."""

from __future__ import annotations


# ruff: noqa: F401

import pickle

from dataclasses import replace

from pathlib import Path

from typing import Any

import pytest

from bioimageflow.engine import (
    DefaultEngine,
    SequentialEngine,
    WorkerTaskError,
    WorkerTimeoutError,
    _compute_engine_timeout,
)

from bioimageflow.workflow import Workflow, WorkflowEnvironment

from bioimageflow_core import (
    EnvironmentSpec,
    ExecutionContext,
    IOModel,
    ProcessingTool,
    RowConsumption,
)


from tests.testkit.worker_timeout import (
    _StubEnvManager,
    _StubTool,
    _execution_contexts,
)


class TestWorkerTimeoutErrorRaised:
    def _make_engine_with_stub(self) -> tuple[DefaultEngine, _StubEnvManager]:
        engine = DefaultEngine(use_wetlands=False)
        engine._use_wetlands = True
        stub = _StubEnvManager()
        engine._env_manager = stub  # type: ignore[assignment]
        return engine, stub

    def test_row_path_raises_worker_timeout_error(self, tmp_path):
        engine, stub = self._make_engine_with_stub()
        tool = _StubTool()
        wf = Workflow(storage_path=tmp_path, engine="direct")
        wf.get_environment(tool).worker_timeout = 10.0
        # Engine will call _resolve_worker_config → (1, None, 10.0)
        # Then map_processing_tasks → hanging tasks → WorkerTimeoutError

        with pytest.raises(WorkerTimeoutError, match="row 0"):
            row_contexts, batch_context = _execution_contexts(2)
            engine._dispatch_via_wetlands(
                tool,
                arguments_dicts=[{"a": 1}, {"a": 2}],
                workflow=wf,
                node_name="my_node",
                has_batch=False,
                row_contexts=row_contexts,
                batch_context=batch_context,
                invocation_id=f"inv_{'1' * 32}",
                cache_attempt_id=f"att_{'2' * 32}",
            )

        # All tasks should have been asked to cancel after timeout
        assert all(t.cancel_called for t in stub.hanging_tasks)
        # worker_timeout should have been passed through
        assert stub.last_worker_timeout == 10.0

    def test_batch_path_raises_worker_timeout_error(self, tmp_path):
        engine, stub = self._make_engine_with_stub()

        class _BatchTool(_StubTool):
            row_consumption = RowConsumption.MAPPED

            def process_batch(self, arguments_list, *, context: object | None = None):
                return []

        tool = _BatchTool()
        wf = Workflow(storage_path=tmp_path, engine="direct")
        wf.get_environment(tool).worker_timeout = 5.0

        with pytest.raises(WorkerTimeoutError, match="Batch"):
            row_contexts, batch_context = _execution_contexts(1)
            engine._dispatch_via_wetlands(
                tool,
                arguments_dicts=[{"a": 1}],
                workflow=wf,
                node_name="my_batch_node",
                has_batch=True,
                row_contexts=row_contexts,
                batch_context=batch_context,
                invocation_id=f"inv_{'1' * 32}",
                cache_attempt_id=f"att_{'2' * 32}",
            )

        assert stub.hanging_tasks[0].cancel_called
        assert stub.last_worker_timeout == 5.0

    def test_no_timeout_when_worker_timeout_none(self, tmp_path):
        """When worker_timeout is None, engine passes timeout=None.

        _HangingTask.wait_for still raises TimeoutError for any timeout,
        so we use a different fake that honors the None case.
        """

        class _PassThroughTask:
            def __init__(self):
                from wetlands import ExecutionState

                self.state = ExecutionState.COMPLETED
                self.result = [{"value": 1.0}]
                self.timeouts_seen: list[float | None] = []

            def wait_for(self, timeout=None):
                self.timeouts_seen.append(timeout)
                return self

            def cancel(self):
                pass

            def listen(self, cb):
                pass

        class _Env:
            def __init__(self):
                self.last_worker_timeout = "sentinel"
                self.tasks: list[_PassThroughTask] = []

            def submit_processing_task(
                self, env_spec, payload, *, worker_timeout=None, **kwargs
            ):
                from bioimageflow_core.worker import execute_processing_task

                self.last_worker_timeout = worker_timeout
                t = _PassThroughTask()
                t.result = execute_processing_task(payload)
                self.tasks.append(t)
                return t

            def map_processing_tasks(
                self, env_spec, payloads, *, worker_timeout=None, **kwargs
            ):
                from bioimageflow_core.worker import execute_processing_task

                self.last_worker_timeout = worker_timeout
                t = _PassThroughTask()
                t.result = execute_processing_task(payloads[0])
                self.tasks.append(t)
                return [t]

            def shutdown_all(self):
                pass

        engine = DefaultEngine(use_wetlands=False)
        engine._use_wetlands = True
        stub = _Env()
        engine._env_manager = stub  # type: ignore[assignment]

        tool = _StubTool()
        wf = Workflow(storage_path=tmp_path, engine="direct")
        # No worker_timeout configured → None flows through

        row_contexts, batch_context = _execution_contexts(1)
        engine._dispatch_via_wetlands(
            tool,
            arguments_dicts=[{"a": 1}],
            workflow=wf,
            node_name="n",
            has_batch=False,
            row_contexts=row_contexts,
            batch_context=batch_context,
            invocation_id=f"inv_{'1' * 32}",
            cache_attempt_id=f"att_{'2' * 32}",
        )
        assert stub.last_worker_timeout is None
        assert stub.tasks[0].timeouts_seen == [None]
