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
    _FailingEnvManager,
    _StubTool,
    _execution_contexts,
)


class TestWorkerTaskErrorRaised:
    def _make_engine_with_failure(
        self,
        exception: BaseException,
    ) -> tuple[DefaultEngine, _FailingEnvManager]:
        engine = DefaultEngine(use_wetlands=False)
        engine._use_wetlands = True
        stub = _FailingEnvManager(exception)
        engine._env_manager = stub  # type: ignore[assignment]
        return engine, stub

    def test_row_path_wraps_failed_task_with_node_context(self, tmp_path):
        original = RuntimeError("native command crashed")
        engine, _stub = self._make_engine_with_failure(original)
        tool = _StubTool()
        wf = Workflow(storage_path=tmp_path, engine="direct")

        with pytest.raises(WorkerTaskError) as exc_info:
            row_contexts, batch_context = _execution_contexts(1)
            row_contexts[0] = replace(row_contexts[0], row_index="sample-A")
            engine._dispatch_via_wetlands(
                tool,
                arguments_dicts=[{"a": 1}],
                workflow=wf,
                node_name="denoise_node",
                has_batch=False,
                row_contexts=row_contexts,
                batch_context=batch_context,
                invocation_id=f"inv_{'1' * 32}",
                cache_attempt_id=f"att_{'2' * 32}",
            )

        message = str(exc_info.value)
        assert "denoise_node" in message
        assert "row sample-A" in message
        assert "_StubTool" in message
        assert "stub_wt_env" in message
        assert "native command crashed" in message
        assert exc_info.value.__cause__ is original
        assert exc_info.value.node_name == "denoise_node"
        assert exc_info.value.row_index == "sample-A"
        assert exc_info.value.tool_class == "_StubTool"
        assert exc_info.value.environment_name == "stub_wt_env"

    def test_batch_path_wraps_failed_task_with_batch_context(self, tmp_path):
        original = ValueError("batch worker failed")
        engine, _stub = self._make_engine_with_failure(original)

        class _BatchTool(_StubTool):
            row_consumption = RowConsumption.MAPPED

            def process_batch(self, arguments_list, *, context: object | None = None):
                return []

        tool = _BatchTool()
        wf = Workflow(storage_path=tmp_path, engine="direct")

        with pytest.raises(WorkerTaskError) as exc_info:
            row_contexts, batch_context = _execution_contexts(1)
            engine._dispatch_via_wetlands(
                tool,
                arguments_dicts=[{"a": 1}],
                workflow=wf,
                node_name="batch_node",
                has_batch=True,
                row_contexts=row_contexts,
                batch_context=batch_context,
                invocation_id=f"inv_{'1' * 32}",
                cache_attempt_id=f"att_{'2' * 32}",
            )

        message = str(exc_info.value)
        assert "batch_node" in message
        assert "batch task" in message
        assert "_BatchTool" in message
        assert "stub_wt_env" in message
        assert "batch worker failed" in message
        assert exc_info.value.__cause__ is original
        assert exc_info.value.row_index is None

    def test_worker_task_error_is_pickle_friendly(self):
        original = RuntimeError("boom")
        error = WorkerTaskError(
            node_name="node",
            tool_class="_StubTool",
            environment_name="stub_wt_env",
            row_index="sample-A",
            original=original,
        )

        restored = pickle.loads(pickle.dumps(error))

        assert str(restored) == str(error)
        assert restored.node_name == "node"
        assert restored.row_index == "sample-A"
        assert isinstance(restored.original, RuntimeError)
