"""Shared helpers for the focused tests split from ``tests/unit/test_worker_timeout.py``."""

from __future__ import annotations


# ruff: noqa: F401

import pickle
import importlib.util
import sys
import uuid

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


@pytest.fixture
def admitted_dispatch_tool(tmp_path):
    """Bind fake dispatch to ordinary tool source and a real compiled node."""
    bindings = []

    def bind(engine, workflow, node_name, *, batch=False):
        name = "timeout_tool_" + uuid.uuid4().hex
        source = tmp_path / (name + ".py")
        source.write_text(
            "from bioimageflow_core import ProcessingTool, IOModel, EnvironmentSpec, RowConsumption\n"
            "class _StubTool(ProcessingTool):\n"
            "    row_consumption = RowConsumption.MAPPED\n"
            "    environment = EnvironmentSpec('stub_wt_env', {})\n"
            "    class Inputs(IOModel): a: int\n"
            "    class Outputs(IOModel): value: float = 0.0\n"
            "    def process_row(self, arguments, *, context=None): return self.Outputs()\n"
            "class _BatchTool(_StubTool):\n"
            "    row_consumption = RowConsumption.MAPPED\n"
            "    def process_batch(self, arguments_list, *, context=None): return []\n"
        )
        spec = importlib.util.spec_from_file_location(name, source)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        owner = workflow.shared_memory_context
        bindings.append((engine, owner, name, module))
        spec.loader.exec_module(module)
        with workflow:
            node = (module._BatchTool if batch else module._StubTool)()(a=1, name=node_name)
        reachable, dependencies, _ = engine._compile_execution_graph([node])
        engine._compiled_ordinals = {
            current: ordinal for ordinal, current in enumerate(engine._topological_sort(reachable, dependencies))
        }
        engine._capture_executable(node)
        return node.tool

    yield bind
    for engine, owner, name, module in reversed(bindings):
        for grant in engine._env_manager.shared_memory_grants:
            grant.drained()
        assert owner.close().state == "closed"
        engine.close()
        if sys.modules.get(name) is module:
            sys.modules.pop(name)


class _StubTool(ProcessingTool):
    row_consumption = RowConsumption.MAPPED
    display_name = "Stub"
    environment = EnvironmentSpec(name="stub_wt_env", dependencies={})

    class Inputs(IOModel):
        pass

    class Outputs(IOModel):
        value: float = 0.0

    def process_row(self, arguments, *, context: object | None = None):
        return self.Outputs()


class _HangingTask:
    """Fake wetlands Task that never reaches a terminal state."""

    def __init__(self) -> None:
        from wetlands import ExecutionState

        self.state = ExecutionState.RUNNING
        self.cancel_called = False

    def wait_for(self, timeout: float | None = None) -> None:
        raise TimeoutError(f"Task did not finish within {timeout}s")

    def cancel(self) -> None:
        from wetlands import ExecutionState

        self.cancel_called = True
        self.state = ExecutionState.CANCELED

    def listen(self, cb: Any) -> None:
        pass


class _FailedTask:
    """Fake Wetlands Task that finished with a worker-side exception."""

    def __init__(self, exception: BaseException) -> None:
        from wetlands import ExecutionState

        self.state = ExecutionState.FAILED
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
        self.shared_memory_grants: list[Any] = []

    def submit_processing_task(
        self,
        env_spec,
        payload,
        max_workers=1,
        worker_timeout=None,
        *,
        shared_memory_grant=None,
        runtime_receipt=None,
    ):
        self.last_worker_timeout = worker_timeout
        if shared_memory_grant is not None:
            self.shared_memory_grants.append(shared_memory_grant)
        t = _HangingTask()
        self.hanging_tasks.append(t)
        return t

    def map_processing_tasks(
        self,
        env_spec,
        payloads,
        max_workers=1,
        worker_timeout=None,
    ):
        self.last_worker_timeout = worker_timeout
        tasks = [_HangingTask() for _ in payloads]
        self.hanging_tasks.extend(tasks)
        return tasks

    def shutdown_all(self) -> None:
        pass


class _FailingEnvManager:
    """Fake WetlandsEnvManager that returns failed tasks."""

    def __init__(self, exception: BaseException) -> None:
        self.exception = exception
        self.tasks: list[_FailedTask] = []
        self.shared_memory_grants: list[Any] = []

    def submit_processing_task(self, *args, **kwargs):
        self.shared_memory_grants.append(kwargs["shared_memory_grant"])
        task = _FailedTask(self.exception)
        self.tasks.append(task)
        return task

    def map_processing_tasks(self, *args, **kwargs):
        task = _FailedTask(self.exception)
        self.tasks.append(task)
        return [task]

    def shutdown_all(self) -> None:
        pass
