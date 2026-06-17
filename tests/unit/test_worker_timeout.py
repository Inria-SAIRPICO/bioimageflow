"""Unit tests for the ``worker_timeout`` feature (Limitation 2).

The Wetlands side is covered in ``wetlands-lib/tests`` — here we only
verify that BioImageFlow plumbs the value through correctly and that the
engine-side safety timeout fires when ``task.wait_for()`` hangs.
"""

from __future__ import annotations

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
from bioimageflow_core import EnvironmentSpec, ExecutionContext, IOModel, ProcessingTool


def _execution_contexts(count: int) -> tuple[list[ExecutionContext], ExecutionContext]:
    run_dir = Path("/tmp/bif_timeout_test")
    assets_dir = run_dir / "assets"
    work_dir = run_dir / "work"
    rows_dir = work_dir / "rows"
    row_contexts = []
    for i in range(count):
        row_dir = rows_dir / f"{i:06d}"
        row_contexts.append(
            ExecutionContext(
                run_dir=run_dir,
                assets_dir=assets_dir,
                work_dir=work_dir,
                rows_dir=rows_dir,
                row_dir=row_dir,
                batch_dir=None,
                row_index=str(i),
            )
        )

    batch_context = ExecutionContext(
        run_dir=run_dir,
        assets_dir=assets_dir,
        work_dir=work_dir,
        rows_dir=rows_dir,
        row_dir=None,
        batch_dir=work_dir / "batch",
    )
    return row_contexts, batch_context


# ── `WorkflowEnvironment.worker_timeout` ─────────────────────────────

class TestWorkflowEnvironmentField:

    def test_default_is_none(self):
        env = WorkflowEnvironment(name="test")
        assert env.worker_timeout is None

    def test_assignment(self):
        env = WorkflowEnvironment(name="test")
        env.worker_timeout = 120.0
        assert env.worker_timeout == 120.0

    def test_explicit_none(self):
        env = WorkflowEnvironment(name="test", worker_timeout=None)
        assert env.worker_timeout is None

    def test_via_get_environment(self):
        spec = EnvironmentSpec(name="test_env", dependencies={})
        wf = Workflow()
        cfg = wf.get_environment(spec)
        assert cfg.worker_timeout is None
        cfg.worker_timeout = 30.0
        assert wf.get_environment(spec).worker_timeout == 30.0


# ── `_compute_engine_timeout` ───────────────────────────────────────

class TestComputeEngineTimeout:

    def test_none_returns_none(self):
        assert _compute_engine_timeout(None) is None

    def test_small_timeout_uses_additive_margin(self):
        # 10s * 1.5 = 15s; 10 + 60 = 70s → max is 70
        assert _compute_engine_timeout(10.0) == 70.0

    def test_large_timeout_uses_multiplicative_margin(self):
        # 200 * 1.5 = 300; 200 + 60 = 260 → max is 300
        assert _compute_engine_timeout(200.0) == 300.0

    def test_boundary(self):
        # 120 * 1.5 = 180; 120 + 60 = 180 → equal
        assert _compute_engine_timeout(120.0) == 180.0


# ── `_resolve_worker_config` returns worker_timeout ─────────────────

class _StubTool(ProcessingTool):
    display_name = "Stub"
    environment = EnvironmentSpec(name="stub_wt_env", dependencies={})

    class Inputs(IOModel):
        pass

    class Outputs(IOModel):
        value: float = 0.0

    def process_row(self, arguments, *, context: object | None = None):
        return self.Outputs()


class TestResolveWorkerConfig:

    def test_default_engine_returns_none_when_not_configured(self):
        engine = DefaultEngine(use_wetlands=False)
        tool = _StubTool()
        wf = Workflow()
        mw, we, wt = engine._resolve_worker_config(tool, wf)
        assert wt is None

    def test_default_engine_returns_configured_timeout(self):
        engine = DefaultEngine(use_wetlands=False)
        tool = _StubTool()
        wf = Workflow()
        wf.get_environment(tool).worker_timeout = 45.0
        mw, we, wt = engine._resolve_worker_config(tool, wf)
        assert wt == 45.0

    def test_sequential_engine_respects_timeout(self):
        engine = SequentialEngine(use_wetlands=False)
        tool = _StubTool()
        wf = Workflow()
        wf.get_environment(tool).worker_timeout = 15.0
        mw, we, wt = engine._resolve_worker_config(tool, wf)
        assert mw == 1
        assert we is None
        assert wt == 15.0


# ── Engine-side safety timeout raises `WorkerTimeoutError` ─────────

class _HangingTask:
    """Fake wetlands Task that never reaches a terminal state."""

    def __init__(self) -> None:
        from wetlands.task import TaskStatus
        self.status = TaskStatus.RUNNING
        self.cancel_called = False

    def wait_for(self, timeout: float | None = None) -> None:
        raise TimeoutError(f"Task did not finish within {timeout}s")

    def cancel(self) -> None:
        self.cancel_called = True

    def listen(self, cb: Any) -> None:
        pass


class _FailedTask:
    """Fake Wetlands Task that finished with a worker-side exception."""

    def __init__(self, exception: BaseException) -> None:
        from wetlands.task import TaskStatus
        self.status = TaskStatus.FAILED
        self.exception = exception
        self.cancel_called = False

    def wait_for(self, timeout: float | None = None) -> None:
        return None

    def cancel(self) -> None:
        self.cancel_called = True

    def listen(self, cb: Any) -> None:
        pass


class _StubEnvManager:
    """Fake WetlandsEnvManager that returns hanging tasks."""

    def __init__(self) -> None:
        self.submitted_batch: list[dict] = []
        self.submitted_rows: list[dict] = []
        self.last_worker_timeout: float | None = None
        self.hanging_tasks: list[_HangingTask] = []

    def submit_process_batch(self, env_spec, tool_file_path, tool_class_name,
                             arguments_dicts, context_dict=None,
                             max_workers=1, worker_env=None,
                             worker_timeout=None):
        self.last_worker_timeout = worker_timeout
        t = _HangingTask()
        self.hanging_tasks.append(t)
        return t

    def map_process_rows(self, env_spec, tool_file_path, tool_class_name,
                         arguments_dicts, context_dicts=None,
                         max_workers=1, worker_env=None,
                         worker_timeout=None):
        self.last_worker_timeout = worker_timeout
        tasks = [_HangingTask() for _ in arguments_dicts]
        self.hanging_tasks.extend(tasks)
        return tasks

    def shutdown_all(self) -> None:
        pass


class _FailingEnvManager:
    """Fake WetlandsEnvManager that returns failed tasks."""

    def __init__(self, exception: BaseException) -> None:
        self.exception = exception
        self.tasks: list[_FailedTask] = []

    def submit_process_batch(self, *args, **kwargs):
        task = _FailedTask(self.exception)
        self.tasks.append(task)
        return task

    def map_process_rows(self, *args, **kwargs):
        task = _FailedTask(self.exception)
        self.tasks.append(task)
        return [task]

    def shutdown_all(self) -> None:
        pass


class TestWorkerTimeoutErrorRaised:

    def _make_engine_with_stub(self) -> tuple[DefaultEngine, _StubEnvManager]:
        engine = DefaultEngine(use_wetlands=False)
        engine._use_wetlands = True
        stub = _StubEnvManager()
        engine._env_manager = stub  # type: ignore[assignment]
        return engine, stub

    def test_row_path_raises_worker_timeout_error(self):
        engine, stub = self._make_engine_with_stub()
        tool = _StubTool()
        wf = Workflow()
        wf.get_environment(tool).worker_timeout = 10.0
        # Engine will call _resolve_worker_config → (1, None, 10.0)
        # Then map_process_rows → hanging tasks → TimeoutError → WorkerTimeoutError

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
            )

        # All tasks should have been asked to cancel after timeout
        assert all(t.cancel_called for t in stub.hanging_tasks)
        # worker_timeout should have been passed through
        assert stub.last_worker_timeout == 10.0

    def test_batch_path_raises_worker_timeout_error(self):
        engine, stub = self._make_engine_with_stub()

        class _BatchTool(_StubTool):
            def process_batch(self, arguments_list, *, context: object | None = None):
                return []

        tool = _BatchTool()
        wf = Workflow()
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
            )

        assert stub.hanging_tasks[0].cancel_called
        assert stub.last_worker_timeout == 5.0

    def test_no_timeout_when_worker_timeout_none(self):
        """When worker_timeout is None, engine passes timeout=None.

        _HangingTask.wait_for still raises TimeoutError for any timeout,
        so we use a different fake that honors the None case.
        """

        class _PassThroughTask:
            def __init__(self):
                from wetlands.task import TaskStatus
                self.status = TaskStatus.COMPLETED
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

            def submit_process_batch(self, *args, worker_timeout=None, **kwargs):
                self.last_worker_timeout = worker_timeout
                t = _PassThroughTask()
                self.tasks.append(t)
                return t

            def map_process_rows(self, *args, worker_timeout=None, **kwargs):
                self.last_worker_timeout = worker_timeout
                t = _PassThroughTask()
                self.tasks.append(t)
                return [t]

            def shutdown_all(self):
                pass

        engine = DefaultEngine(use_wetlands=False)
        engine._use_wetlands = True
        stub = _Env()
        engine._env_manager = stub  # type: ignore[assignment]

        tool = _StubTool()
        wf = Workflow()
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
        )
        assert stub.last_worker_timeout is None
        assert stub.tasks[0].timeouts_seen == [None]


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

    def test_row_path_wraps_failed_task_with_node_context(self):
        original = RuntimeError("native command crashed")
        engine, _stub = self._make_engine_with_failure(original)
        tool = _StubTool()
        wf = Workflow()

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

    def test_batch_path_wraps_failed_task_with_batch_context(self):
        original = ValueError("batch worker failed")
        engine, _stub = self._make_engine_with_failure(original)

        class _BatchTool(_StubTool):
            def process_batch(self, arguments_list, *, context: object | None = None):
                return []

        tool = _BatchTool()
        wf = Workflow()

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


# ── End-to-end plumbing through Workflow and WetlandsEnvManager ─────

class TestWorkerTimeoutPlumbing:
    """Verify the value flows from user code → env_manager.launch()."""

    def test_env_manager_forwards_worker_timeout_to_launch(self, monkeypatch):
        """``WetlandsEnvManager.get_or_create`` passes ``worker_timeout`` to
        ``env.launch(...)``."""
        from bioimageflow.env_manager import WetlandsEnvManager

        # Patch the shared environment manager so get_or_create doesn't try
        # to actually create a conda env.
        launch_calls: list[dict] = []

        class _FakeEnv:
            def launch(self, **kwargs):
                launch_calls.append(kwargs)

        class _FakeManager:
            def create(self, name, deps):
                return _FakeEnv()

        monkeypatch.setattr(
            "bioimageflow.env_manager.get_shared_environment_manager",
            lambda **kw: _FakeManager(),
        )

        mgr = WetlandsEnvManager()
        spec = EnvironmentSpec(name="wt_plumb", dependencies={"pip": []})
        mgr.get_or_create(spec, max_workers=1, worker_timeout=17.0)

        assert len(launch_calls) == 1
        assert launch_calls[0].get("worker_timeout") == 17.0

    def test_env_manager_omits_worker_timeout_when_none(self, monkeypatch):
        """When ``worker_timeout=None``, do not pass the kwarg at all.

        This keeps the manager compatible with Wetlands versions that do not
        accept the keyword.
        """
        from bioimageflow.env_manager import WetlandsEnvManager

        launch_calls: list[dict] = []

        class _FakeEnv:
            def launch(self, **kwargs):
                launch_calls.append(kwargs)

        class _FakeManager:
            def create(self, name, deps):
                return _FakeEnv()

        monkeypatch.setattr(
            "bioimageflow.env_manager.get_shared_environment_manager",
            lambda **kw: _FakeManager(),
        )

        mgr = WetlandsEnvManager()
        spec = EnvironmentSpec(name="wt_plumb_none", dependencies={"pip": []})
        mgr.get_or_create(spec, max_workers=1, worker_timeout=None)

        assert len(launch_calls) == 1
        assert "worker_timeout" not in launch_calls[0]
