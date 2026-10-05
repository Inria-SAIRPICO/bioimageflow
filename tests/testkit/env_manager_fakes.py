"""Small public-manager fixtures for environment translation and recreation controls."""
from __future__ import annotations

import threading
from types import SimpleNamespace
from typing import Any

from wetlands import EnvironmentNotReadyError
from bioimageflow.env_manager import WetlandsEnvManager

def _manager_with_core_dependency(dependency: object) -> WetlandsEnvManager:
    manager = object.__new__(WetlandsEnvManager)
    manager._bioimageflow_core_dependency = dependency
    return manager


class _Pool:
    def __init__(self) -> None:
        self.close_count = 0

    def close(self) -> None:
        self.close_count += 1


class _MutatingWetlandsEnvironment:
    def __init__(self) -> None:
        self.start_count = 0
        self.generation_id = "mutating-generation"
        self.pool = _Pool()

    def start(self, **kwargs: Any) -> _Pool:
        self.start_count += 1
        self.start_kwargs = kwargs
        return self.pool


class _Operation:
    def __init__(self, value: Any) -> None:
        self.value = value

    def listen(self, callback: Any) -> None:
        self.callback = callback

    def wait_for(self) -> Any:
        if hasattr(self, "callback"):
            self.callback("pixi installed")
        return self.value

    def wait_for_completion(self, timeout: float | None = None) -> None:
        assert timeout is None

    def cancel(self) -> bool:
        return False

    def remove_listener(self, callback: Any) -> None:
        if getattr(self, "callback", None) is callback:
            del self.callback


class _MutatingWetlandsManager:
    def __init__(self) -> None:
        self.provisioned_specs: list[Any] = []
        self.replace_existing: list[bool] = []
        self.env = _MutatingWetlandsEnvironment()
        self.infos: tuple[Any, ...] = ()

    def environment(self, name: str) -> _MutatingWetlandsEnvironment:
        if not any(info.name == name for info in self.infos):
            raise EnvironmentNotReadyError(name)
        return self.env

    def managed_environments(self) -> tuple[Any, ...]:
        return self.infos

    def provision(
        self, name: str, spec: Any, *, replace_existing: bool = False
    ) -> _Operation:
        _ = name, replace_existing
        self.provisioned_specs.append(spec)
        self.replace_existing.append(replace_existing)
        self.infos = (
            SimpleNamespace(
                name=name,
                ready=True,
                recipe_hash=spec.recipe_hash,
            ),
        )
        return _Operation(self.env)


def _runtime_manager_with_core_dependency(dependency: object) -> WetlandsEnvManager:
    manager = _manager_with_core_dependency(dependency)
    manager._manager = _MutatingWetlandsManager()
    manager._environments = {}
    manager._pools = {}
    manager._shared_memory_grants = {}
    manager._processing_tasks = {}
    manager._pool_configs = {}
    manager._specs = {}
    manager._lock = threading.RLock()
    return manager


class _GenerationPool(_Pool):
    def __init__(self, generation_id: int) -> None:
        super().__init__()
        self.generation_id = generation_id
        self.close_failure: RuntimeError | None = None

    def close(self) -> None:
        super().close()
        if self.close_failure is not None:
            raise self.close_failure


class _GenerationEnvironment(_MutatingWetlandsEnvironment):
    def __init__(self, name: str, recipe_hash: str, generation_id: int) -> None:
        super().__init__()
        self.name, self.recipe_hash = name, recipe_hash
        self.generation_id = str(generation_id)
        self.pool = _GenerationPool(generation_id)
        self.pool.environment = self


class _GenerationManager:
    """Public manager fake: matching provisioning reuses even with replacement permission."""

    def __init__(self) -> None:
        self.environments: dict[str, _GenerationEnvironment] = {}
        self.provisioned_specs: list[Any] = []
        self.replace_existing: list[bool] = []
        self.remove_calls: list[str] = []
        self.generation = 0

    def environment(self, name: str) -> _GenerationEnvironment:
        if name not in self.environments:
            raise EnvironmentNotReadyError(name)
        return self.environments[name]

    def managed_environments(self) -> tuple[Any, ...]:
        return tuple(SimpleNamespace(name=env.name, ready=True,
                                     recipe_hash=env.recipe_hash,
                                     generation_id=env.generation_id)
                     for env in self.environments.values())

    def provision(self, name: str, spec: Any, *, replace_existing: bool = False) -> _Operation:
        self.provisioned_specs.append(spec)
        self.replace_existing.append(replace_existing)
        env = self.environments.get(name)
        if env is None or env.recipe_hash != spec.recipe_hash:
            self.generation += 1
            env = self.environments[name] = _GenerationEnvironment(name, spec.recipe_hash, self.generation)
        return _Operation(env)

    def remove(self, name: str) -> _Operation:
        env = self.environment(name)
        if env.start_count and not env.pool.close_count:
            raise RuntimeError("selected public pool is still open")
        self.remove_calls.append(name)
        del self.environments[name]
        return _Operation(env)


def _generation_runtime_manager() -> tuple[WetlandsEnvManager, _GenerationManager]:
    wrapper = _runtime_manager_with_core_dependency("bioimageflow-core==0.5.0")
    public = _GenerationManager()
    wrapper._manager = public
    return wrapper, public

