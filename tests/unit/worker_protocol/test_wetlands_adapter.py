"""Wetlands transport use of the shared strict worker protocol."""

from __future__ import annotations

import threading
from wetlands import ExecutionState

from bioimageflow.env_manager import WetlandsEnvManager
from bioimageflow_core import EnvironmentSpec


class _Task:
    state = ExecutionState.COMPLETED


class _Pool:
    def __init__(self) -> None:
        self.submission = None
        self.submissions = []
        self.tasks = []

    def submit_import(self, target, *, args, context_keyword=None):
        self.submission = (target, args, context_keyword)
        self.submissions.append(self.submission)
        task = _Task()
        self.tasks.append(task)
        return task


def _manager(environment: _Pool) -> WetlandsEnvManager:
    manager = object.__new__(WetlandsEnvManager)
    manager._lock = threading.RLock()
    manager._processing_tasks = {}
    manager.get_or_create = lambda *args, **kwargs: environment
    return manager


def test_wetlands_submits_the_canonical_processing_entry_point() -> None:
    environment = _Pool()
    manager = _manager(environment)
    payload = {"schema": "bioimageflow.processing_task.v4"}

    result = manager.submit_processing_task(
        EnvironmentSpec(name="worker", dependencies={}),
        payload,
    )

    assert result is environment.tasks[0]
    assert environment.submission == (
        "bioimageflow_core.worker:execute_processing_task",
        (payload,),
        "task",
    )


def test_wetlands_maps_the_same_canonical_processing_entry_point() -> None:
    environment = _Pool()
    manager = _manager(environment)
    payloads = [
        {"schema": "bioimageflow.processing_task.v4", "task_id": "task_0"},
        {"schema": "bioimageflow.processing_task.v4", "task_id": "task_1"},
    ]

    result = manager.map_processing_tasks(
        EnvironmentSpec(name="worker", dependencies={}),
        payloads,
    )

    assert len(result) == 2 and all(item is task for item, task in zip(result, environment.tasks))
    assert environment.submissions == [
        ("bioimageflow_core.worker:execute_processing_task", (payload,), "task")
        for payload in payloads
    ]
