"""Shared helpers for the focused tests split from ``tests/unit/test_worker_timeout.py``."""

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


class _StubTool(ProcessingTool):
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

    def submit_process_batch(
        self,
        env_spec,
        tool_file_path,
        tool_class_name,
        arguments_dicts,
        context_dict=None,
        max_workers=1,
        worker_env=None,
        worker_timeout=None,
    ):
        self.last_worker_timeout = worker_timeout
        t = _HangingTask()
        self.hanging_tasks.append(t)
        return t

    def map_process_rows(
        self,
        env_spec,
        tool_file_path,
        tool_class_name,
        arguments_dicts,
        context_dicts=None,
        max_workers=1,
        worker_env=None,
        worker_timeout=None,
    ):
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
