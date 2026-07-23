"""Wetlands transport use of the shared strict worker protocol."""

from __future__ import annotations

from bioimageflow.env_manager import WetlandsEnvManager
from bioimageflow_core import EnvironmentSpec


class _Environment:
    def __init__(self) -> None:
        self.submission = None
        self.mapping = None

    def submit(self, worker_file, function_name, *, args):
        self.submission = (worker_file, function_name, args)
        return "submitted"

    def map_tasks(self, worker_file, function_name, payloads):
        self.mapping = (worker_file, function_name, payloads)
        return ["mapped"]


def _manager(environment: _Environment) -> WetlandsEnvManager:
    manager = object.__new__(WetlandsEnvManager)
    manager._worker_file = "/shared/bioimageflow_core/worker.py"
    manager.get_or_create = lambda *args, **kwargs: environment
    return manager


def test_wetlands_submits_the_canonical_processing_entry_point() -> None:
    environment = _Environment()
    manager = _manager(environment)
    payload = {"schema": "bioimageflow.processing_task.v1"}

    result = manager.submit_processing_task(
        EnvironmentSpec(name="worker", dependencies={}),
        payload,
    )

    assert result == "submitted"
    assert environment.submission == (
        "/shared/bioimageflow_core/worker.py",
        "execute_processing_task",
        (payload,),
    )


def test_wetlands_maps_the_same_canonical_processing_entry_point() -> None:
    environment = _Environment()
    manager = _manager(environment)
    payloads = [
        {"schema": "bioimageflow.processing_task.v1", "task_id": "task_0"},
        {"schema": "bioimageflow.processing_task.v1", "task_id": "task_1"},
    ]

    result = manager.map_processing_tasks(
        EnvironmentSpec(name="worker", dependencies={}),
        payloads,
    )

    assert result == ["mapped"]
    assert environment.mapping == (
        "/shared/bioimageflow_core/worker.py",
        "execute_processing_task",
        payloads,
    )
