"""Public execution-resource lifecycle API tests."""

from __future__ import annotations

import threading
from types import SimpleNamespace
from typing import Any, cast

import pytest

from bioimageflow import (
    DefaultEngine,
    ResourceLifetime,
    SequentialEngine,
    WetlandsEnvManager,
    Workflow,
)
from bioimageflow_core import EnvironmentSpec
from bioimageflow.backends import WetlandsBackend
from bioimageflow.engine import WorkflowCancelledError


class _TrackingManager:
    def __init__(self) -> None:
        self.shutdown_calls = 0

    def shutdown_all(self) -> None:
        self.shutdown_calls += 1


class _HarnessEngine(DefaultEngine):
    def __init__(
        self,
        manager: _TrackingManager | WetlandsEnvManager,
        *,
        lifetime: ResourceLifetime | str = ResourceLifetime.EXECUTION,
        error: Exception | None = None,
    ) -> None:
        super().__init__()
        self._use_wetlands = True
        self._backend = WetlandsBackend()
        self._resource_lifetime = ResourceLifetime(lifetime)
        self._env_manager = cast(WetlandsEnvManager, manager)
        self.error = error
        self.on_execute: Any = None

    def _execute_impl(self, targets: Any, workflow: Any) -> dict[str, Any]:
        if self.on_execute is not None:
            self.on_execute()
        if self.error is not None:
            raise self.error
        return {}


class _SequentialHarness(SequentialEngine):
    def __init__(
        self,
        manager: _TrackingManager,
        *,
        lifetime: ResourceLifetime | str,
    ) -> None:
        super().__init__()
        self._use_wetlands = True
        self._backend = WetlandsBackend()
        self._resource_lifetime = ResourceLifetime(lifetime)
        self._env_manager = cast(WetlandsEnvManager, manager)

    def _execute_impl(self, targets: Any, workflow: Any) -> dict[str, Any]:
        return {}


class _FakeWetlandsEnvironment:
    def __init__(self, *, fail_on_exit: bool = False) -> None:
        self.launch_calls: list[dict[str, Any]] = []
        self.exit_calls = 0
        self.fail_on_exit = fail_on_exit

    def launch(self, **kwargs: Any) -> None:
        self.launch_calls.append(kwargs)

    def exit(self) -> None:
        self.exit_calls += 1
        if self.fail_on_exit:
            raise RuntimeError("worker exit failed")


class _FakeWetlandsBackend:
    def __init__(self, env: _FakeWetlandsEnvironment) -> None:
        self.env = env
        self.create_calls = 0

    def create(self, name: str, dependencies: Any) -> _FakeWetlandsEnvironment:
        self.create_calls += 1
        return self.env


def _bare_manager(
    envs: dict[str, _FakeWetlandsEnvironment] | None = None,
) -> WetlandsEnvManager:
    manager = object.__new__(WetlandsEnvManager)
    manager._envs = dict(envs or {})
    manager._launch_configs = {name: (1, None, None) for name in manager._envs}
    manager._lock = threading.RLock()
    return manager


@pytest.mark.parametrize("error", [None, RuntimeError("execution failed")])
def test_default_execution_lifetime_shuts_down_after_execute(
    error: Exception | None,
) -> None:
    manager = _TrackingManager()
    engine = _HarnessEngine(manager, error=error)

    if error is None:
        engine.execute([], object())
    else:
        with pytest.raises(RuntimeError, match="execution failed"):
            engine.execute([], object())

    assert engine.resource_lifetime is ResourceLifetime.EXECUTION
    assert manager.shutdown_calls == 1


@pytest.mark.parametrize("error", [None, RuntimeError("execution failed")])
def test_engine_lifetime_retains_after_success_and_failure(
    error: Exception | None,
) -> None:
    manager = _TrackingManager()
    engine = _HarnessEngine(manager, lifetime="engine", error=error)

    if error is None:
        engine.execute([], object())
    else:
        with pytest.raises(RuntimeError, match="execution failed"):
            engine.execute([], object())

    assert manager.shutdown_calls == 0
    engine.close()
    assert manager.shutdown_calls == 1


def test_engine_lifetime_retains_after_cancellation_until_close() -> None:
    manager = _TrackingManager()
    engine = _HarnessEngine(
        manager,
        lifetime="engine",
        error=WorkflowCancelledError("cancelled"),
    )

    with pytest.raises(WorkflowCancelledError, match="cancelled"):
        engine.execute([], object())

    assert manager.shutdown_calls == 0
    engine.close()
    assert manager.shutdown_calls == 1


def test_execute_steps_uses_engine_lifetime_policy() -> None:
    manager = _TrackingManager()
    engine = _HarnessEngine(manager, lifetime="engine")

    assert list(engine.execute_steps([], SimpleNamespace(cancel_requested=False))) == []
    assert manager.shutdown_calls == 0

    engine.close()
    assert manager.shutdown_calls == 1


def test_execute_steps_default_lifetime_shuts_down_on_completion() -> None:
    manager = _TrackingManager()
    engine = _HarnessEngine(manager)

    assert list(engine.execute_steps([], SimpleNamespace(cancel_requested=False))) == []
    assert manager.shutdown_calls == 1


def test_repeated_engine_executions_launch_environment_once() -> None:
    environment = _FakeWetlandsEnvironment()
    backend = _FakeWetlandsBackend(environment)
    manager = _bare_manager()
    manager._manager = backend
    manager._bioimageflow_core_dependency = "bioimageflow-core==0"
    manager._worker_file = "worker.py"
    spec = EnvironmentSpec(
        name="warm",
        dependencies={"pip": ["bioimageflow-core==0"]},
    )
    engine = _HarnessEngine(manager, lifetime="engine")
    engine.on_execute = lambda: manager.get_or_create(spec)

    engine.execute([], object())
    engine.execute([], object())

    assert environment.launch_calls == [{}]
    assert manager.running_environments() == ("warm",)
    engine.close()
    assert environment.exit_calls == 1


def test_close_is_idempotent_and_closed_engine_cannot_execute() -> None:
    manager = _TrackingManager()
    engine = _HarnessEngine(manager, lifetime="engine")

    engine.close()
    engine.close()

    assert manager.shutdown_calls == 1
    with pytest.raises(RuntimeError, match="closed"):
        engine.execute([], object())


def test_context_manager_exit_closes_engine_after_error() -> None:
    manager = _TrackingManager()

    with pytest.raises(RuntimeError, match="body failed"):
        with _HarnessEngine(manager, lifetime="engine"):
            raise RuntimeError("body failed")

    assert manager.shutdown_calls == 1


def test_external_manager_is_never_shut_down_by_engine() -> None:
    manager = _TrackingManager()
    engine = _HarnessEngine(manager, lifetime="external")

    engine.execute([], object())
    engine.close()

    assert engine.environment_manager is manager
    assert manager.shutdown_calls == 0


def test_resource_lifetime_validation() -> None:
    with pytest.raises(ValueError, match="resource_lifetime"):
        DefaultEngine(resource_lifetime="forever")

    with pytest.raises(ValueError, match="injected env_manager"):
        DefaultEngine(use_wetlands=True, resource_lifetime="external")

    with pytest.raises(ValueError, match="use_wetlands=True"):
        DefaultEngine(env_manager=cast(WetlandsEnvManager, _TrackingManager()))

    with pytest.raises(ValueError, match="requires resource_lifetime='external'"):
        DefaultEngine(
            use_wetlands=True,
            env_manager=cast(WetlandsEnvManager, _TrackingManager()),
        )

    with pytest.raises(ValueError, match="Direct execution"):
        DefaultEngine(resource_lifetime="engine")


def test_manager_stop_and_status_introspection() -> None:
    first = _FakeWetlandsEnvironment()
    second = _FakeWetlandsEnvironment(fail_on_exit=True)
    manager = _bare_manager({"zeta": second, "alpha": first})

    assert manager.running_environments() == ("alpha", "zeta")
    assert manager.is_running("alpha") is True
    assert manager.stop("alpha") is True
    assert manager.stop("alpha") is False
    assert manager.is_running("alpha") is False
    assert first.exit_calls == 1

    manager.shutdown_all()
    manager.shutdown_all()

    assert manager.running_environments() == ()
    assert second.exit_calls == 1


def test_sequential_engine_honors_engine_lifetime() -> None:
    manager = _TrackingManager()
    engine = _SequentialHarness(manager, lifetime="engine")

    engine.execute([], object())

    assert engine._force_sequential is True
    assert manager.shutdown_calls == 0
    engine.close()
    assert manager.shutdown_calls == 1


def test_workflow_public_factory_preserves_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created: list[dict[str, Any]] = []

    class FakeManager(_TrackingManager):
        def __init__(self, **kwargs: Any) -> None:
            super().__init__()
            created.append(kwargs)

    monkeypatch.setattr("bioimageflow.env_manager.WetlandsEnvManager", FakeManager)
    workflow = Workflow(
        execution="sequential",
        wetlands_config={"debug": True},
        max_workers=4,
    )

    engine = workflow.create_engine(resource_lifetime="engine")

    assert isinstance(engine, SequentialEngine)
    assert engine._use_wetlands is True
    assert engine._force_sequential is True
    assert engine.resource_lifetime is ResourceLifetime.ENGINE
    assert created == [{"debug": True}]
    assert workflow.max_workers == 4
    engine.close()


def test_workflow_factory_injects_external_manager() -> None:
    manager = _TrackingManager()
    workflow = Workflow()

    engine = workflow.create_engine(
        env_manager=cast(WetlandsEnvManager, manager),
        resource_lifetime=ResourceLifetime.EXTERNAL,
    )
    engine.close()

    assert type(engine) is DefaultEngine
    assert engine.environment_manager is manager
    assert manager.shutdown_calls == 0
