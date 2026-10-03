"""Strict same-manager recreation and public operation completion ownership."""
from __future__ import annotations

import importlib.metadata
from typing import Any
from types import SimpleNamespace

import pytest
from wetlands import EnvironmentNotReadyError, OperationState
from bioimageflow_core import EnvironmentSpec
from tests.testkit.env_manager_fakes import _Operation, _generation_runtime_manager

def test_get_or_create_explicitly_recreates_current_cached_recipe() -> None:
    manager, public = _generation_runtime_manager()
    spec = EnvironmentSpec(name="segment", dependencies={"python": "3.11"})
    preparations: list[str] = []
    old_pool = manager.get_or_create(spec)
    assert manager.get_or_create(spec) is old_pool
    assert public.replace_existing == [False]
    unrelated_spec = EnvironmentSpec(name="unrelated", dependencies={"python": "3.11"})
    unrelated_pool = manager.get_or_create(unrelated_spec)
    unrelated_environment = public.environment(unrelated_spec.name)
    unrelated_generation = unrelated_environment.generation_id

    new_pool = manager.get_or_create(
        spec, replace_existing=True,
        on_preparation=lambda event: preparations.append(event.action),
    )

    assert new_pool is not old_pool
    assert new_pool.environment.generation_id != old_pool.environment.generation_id
    assert old_pool.close_count == 1
    assert public.remove_calls == ["segment"]
    assert public.replace_existing == [False, False, False]
    assert preparations == ["updating"]
    assert manager.get_or_create(spec) is new_pool
    assert manager.get_or_create(unrelated_spec) is unrelated_pool
    assert public.environment(unrelated_spec.name) is unrelated_environment
    assert unrelated_environment.generation_id == unrelated_generation
    assert unrelated_pool.close_count == 0


def test_forced_recreate_close_failure_retains_owner_without_lifecycle_effects() -> None:
    manager, public = _generation_runtime_manager()
    spec = EnvironmentSpec(name="segment", dependencies={"python": "3.11"})
    old_pool = manager.get_or_create(spec)
    old_environment = public.environment(spec.name)
    old_config = manager._pool_configs[spec.name]
    old_spec = manager._specs[spec.name]
    class Grant:
        drain_count = 0
        def drained(self):
            self.drain_count += 1
    selected_grant, unrelated_grant = Grant(), Grant()
    manager._shared_memory_grants[spec.name] = [selected_grant]
    manager._shared_memory_grants["unrelated"] = [unrelated_grant]
    old_pool.close_failure = RuntimeError("controlled pool close failure")

    with pytest.raises(RuntimeError, match="controlled pool close failure") as failure:
        manager.get_or_create(spec, replace_existing=True)

    assert failure.value is old_pool.close_failure
    assert manager._pools[spec.name] is old_pool
    assert manager._environments[spec.name] is old_environment
    assert manager._pool_configs[spec.name] == old_config
    assert manager._specs[spec.name] is old_spec
    assert manager.is_running(spec.name)
    assert public.remove_calls == []
    assert public.replace_existing == [False]
    assert manager._shared_memory_grants[spec.name] == [selected_grant]
    assert selected_grant.drain_count == unrelated_grant.drain_count == 0
    assert old_pool.close_count == 1

    old_pool.close_failure = None
    retried_pool = manager.get_or_create(spec, replace_existing=True)
    assert retried_pool is not old_pool
    assert retried_pool.environment.generation_id != old_environment.generation_id
    assert old_pool.close_count == 2
    assert public.remove_calls == ["segment"]
    assert public.replace_existing == [False, False]
    assert manager._pools[spec.name] is retried_pool
    assert manager._environments[spec.name] is public.environment(spec.name)
    assert selected_grant.drain_count == 1
    assert unrelated_grant.drain_count == 0
    assert manager._shared_memory_grants["unrelated"] == [unrelated_grant]


@pytest.mark.parametrize("phase,delivery", [
    ("removal", "replay"), ("provision", "replay"), ("provision", "terminal"),
])
def test_recreate_drains_listener_replay_interruption_before_propagating(
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
    delivery: str,
) -> None:
    manager, public = _generation_runtime_manager()
    spec = EnvironmentSpec(name="segment", dependencies={"python": "3.11"})
    manager.get_or_create(spec)
    original_interruption = KeyboardInterrupt("listener replay interrupted")

    class InterruptedOperation(_Operation):
        state = OperationState.COMPLETED
        cancel_calls = 0
        wait_calls = 0
        completion_calls = 0
        drain_completed = False
        listener: Any = None
        removed_listener: Any = None

        def listen(self, callback: Any) -> None:
            self.listener = callback
            if delivery == "replay":
                callback("terminal event replay")

        def remove_listener(self, callback: Any) -> None:
            assert callback is self.listener or callback is self.removed_listener
            self.removed_listener = callback
            self.listener = None

        def cancel(self) -> bool:
            self.cancel_calls += 1
            assert self.listener is None
            raise KeyboardInterrupt("cancellation interrupted")

        def wait_for(self) -> Any:
            self.wait_calls += 1
            assert delivery == "terminal"
            self.listener("runner terminal notification")
            return self.value

        def wait_for_completion(self, timeout: float | None = None) -> None:
            assert timeout is None
            self.completion_calls += 1
            if self.completion_calls == 1:
                raise KeyboardInterrupt("completion wait interrupted")
            self.drain_completed = True

    operations: list[InterruptedOperation] = []
    original = public.remove if phase == "removal" else public.provision

    def interrupted_operation(*args: Any, **kwargs: Any) -> InterruptedOperation:
        operation = InterruptedOperation(original(*args, **kwargs).value)
        operations.append(operation)
        return operation

    monkeypatch.setattr(public, "remove" if phase == "removal" else "provision", interrupted_operation)

    def interrupt_replay(_event: Any) -> None:
        raise original_interruption

    callbacks = (
        {"on_removal_event": interrupt_replay}
        if phase == "removal"
        else {"on_provision_event": interrupt_replay}
    )
    with pytest.raises(KeyboardInterrupt) as failure:
        manager.recreate(spec, **callbacks)

    assert failure.value is original_interruption
    assert len(operations) == 1
    assert operations[0].cancel_calls == 1
    assert operations[0].wait_calls == (1 if delivery == "terminal" else 0)
    assert operations[0].completion_calls == 2
    assert operations[0].drain_completed
    assert operations[0].listener is None
    assert operations[0].removed_listener is not None
    assert not manager.is_running(spec.name)
    assert public.remove_calls == [spec.name]
    if phase == "removal":
        assert public.replace_existing == [False]
    else:
        assert public.replace_existing == [False, False]
        assert public.environment(spec.name).start_count == 0


def test_recreate_preserves_owner_error_when_completed_wait_repeats_stored_interruption(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, public = _generation_runtime_manager()
    spec = EnvironmentSpec(name="segment", dependencies={"python": "3.11"})
    manager.get_or_create(spec)
    owner_error = KeyboardInterrupt("owner listener interrupted")
    stored_error = KeyboardInterrupt("stored terminal operation failure")

    class FailedOperation(_Operation):
        state = OperationState.FAILED
        wait_calls = 0
        completion_calls = 0
        cancel_calls = 0
        listener: Any = None

        def listen(self, callback: Any) -> None:
            self.listener = callback

        def remove_listener(self, callback: Any) -> None:
            assert self.listener is None or self.listener is callback
            self.listener = None

        def cancel(self) -> bool:
            assert self.listener is None
            self.cancel_calls += 1
            return False

        def wait_for(self) -> Any:
            self.wait_calls += 1
            assert self.wait_calls == 1, "stored outcome must not be used for drain"
            self.listener("runner terminal notification")
            raise stored_error

        def wait_for_completion(self, timeout: float | None = None) -> None:
            assert timeout is None
            self.completion_calls += 1

    original_remove = public.remove
    operations: list[FailedOperation] = []

    def failed_remove(name: str) -> FailedOperation:
        operation = FailedOperation(original_remove(name).value)
        operations.append(operation)
        return operation

    monkeypatch.setattr(public, "remove", failed_remove)

    def interrupted_listener(_event: Any) -> None:
        raise owner_error

    with pytest.raises(KeyboardInterrupt) as failure:
        manager.recreate(spec, on_removal_event=interrupted_listener)

    assert failure.value is owner_error
    assert operations[0].wait_calls == 1
    assert operations[0].completion_calls == 1
    assert operations[0].cancel_calls == 1
    assert not manager.is_running(spec.name)
    assert public.replace_existing == [False]


def test_recreate_rejects_unsupported_wetlands_before_effects_but_keeps_warm_reuse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, public = _generation_runtime_manager()
    spec = EnvironmentSpec(name="segment", dependencies={"python": "3.11"})
    pool = manager.get_or_create(spec)
    environment = public.environment(spec.name)
    previous_version = importlib.metadata.version
    monkeypatch.setattr(
        importlib.metadata, "version",
        lambda name: "2.4.3" if name == "wetlands" else previous_version(name),
    )
    preparations: list[Any] = []

    with pytest.raises(RuntimeError, match="Wetlands.*2.5"):
        manager.recreate(spec, on_preparation=preparations.append)

    assert preparations == []
    assert pool.close_count == 0
    assert public.remove_calls == []
    assert public.replace_existing == [False]
    assert manager.get_or_create(spec) is pool
    assert public.environment(spec.name) is environment



def test_recreate_preflights_recipe_and_start_arguments_before_preparation() -> None:
    manager, public = _generation_runtime_manager()
    spec = EnvironmentSpec(name="segment", dependencies={"python": "3.11"})
    pool = manager.get_or_create(spec)
    preparations: list[Any] = []
    invalid_recipe = EnvironmentSpec(name=spec.name, dependencies={"unsupported": []})
    with pytest.raises(ValueError, match="Unsupported"):
        manager.recreate(invalid_recipe, on_preparation=preparations.append)
    with pytest.raises(ValueError, match="max_workers"):
        manager.recreate(spec, max_workers=0, on_preparation=preparations.append)
    with pytest.raises(ValueError, match="worker_timeout"):
        manager.recreate(spec, worker_timeout=float("nan"), on_preparation=preparations.append)
    assert preparations == []
    assert pool.close_count == 0
    assert public.remove_calls == []
    assert manager.get_or_create(spec) is pool


def test_recreate_uses_captured_recipe_core_and_start_arguments_through_callback() -> None:
    manager, public = _generation_runtime_manager()
    spec = EnvironmentSpec(name="segment", dependencies={"python": "3.11"})
    manager.get_or_create(spec)

    def mutate_inputs(_preparation: Any) -> None:
        spec.dependencies["python"] = "3.12"
        manager._bioimageflow_core_dependency = "bioimageflow-core==0.5.0.dev1"

    pool = manager.recreate(spec, max_workers=2, worker_timeout=7,
                            on_preparation=mutate_inputs)
    assert public.remove_calls == ["segment"]
    assert public.provisioned_specs[-1].python == "3.11.*"
    assert public.provisioned_specs[-1].pypi == ("bioimageflow-core==0.5.0",)
    assert pool.environment.start_kwargs == {"workers": 2, "worker_timeout": 7}
    assert manager.is_running("segment")


@pytest.mark.parametrize("phase", ["removal", "provision", "start"])
def test_recreate_failed_phase_does_not_publish_pool_and_can_retry(
    monkeypatch: pytest.MonkeyPatch, phase: str,
) -> None:
    manager, public = _generation_runtime_manager()
    spec = EnvironmentSpec(name="segment", dependencies={"python": "3.11"})
    pool = manager.get_or_create(spec)
    other = manager.get_or_create(EnvironmentSpec(name="other", dependencies={"python": "3.11"}))
    error = RuntimeError(f"controlled {phase} failure")

    class FailedOperation(_Operation):
        def wait_for(self) -> Any:
            raise error

    with monkeypatch.context() as patch:
        if phase == "start":
            def fail_start(_environment: Any, **_kwargs: Any) -> Any:
                raise error
            patch.setattr(type(pool.environment), "start", fail_start)
        else:
            method = "remove" if phase == "removal" else "provision"
            original = getattr(public, method)
            def failed_operation(*args: Any, **kwargs: Any) -> FailedOperation:
                return FailedOperation(original(*args, **kwargs).value)
            patch.setattr(public, method, failed_operation)
        with pytest.raises(RuntimeError) as failure:
            manager.recreate(spec)
        assert failure.value is error
        assert not manager.is_running(spec.name)
        assert spec.name not in manager._environments
        assert spec.name not in manager._pool_configs
        assert spec.name not in manager._specs
        assert public.environment("other").pool is other
        assert other.close_count == 0
        if phase == "removal":
            assert len(public.provisioned_specs) == 2
        elif phase == "provision":
            assert public.environment(spec.name).start_count == 0
    retried = manager.recreate(spec)
    assert manager.is_running(spec.name)
    assert manager.get_or_create(spec) is retried
    assert retried is not pool


def test_recreate_removes_incomplete_managed_target_and_creates_missing_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, public = _generation_runtime_manager()
    other = manager.get_or_create(EnvironmentSpec(name="other", dependencies={"python": "3.11"}))
    incomplete = [True]
    inventory = public.managed_environments
    remove = public.remove

    def managed_environments() -> tuple[Any, ...]:
        extra = (SimpleNamespace(name="incomplete", ready=False, recipe_hash=None),) if incomplete[0] else ()
        return inventory() + extra

    def remove_managed(name: str) -> _Operation:
        if name == "incomplete" and incomplete[0]:
            public.remove_calls.append(name)
            incomplete[0] = False
            return _Operation(None)
        return remove(name)

    monkeypatch.setattr(public, "managed_environments", managed_environments)
    monkeypatch.setattr(public, "remove", remove_managed)
    with pytest.raises(EnvironmentNotReadyError):
        public.environment("incomplete")
    rebuilt = manager.recreate(EnvironmentSpec(name="incomplete", dependencies={"python": "3.11"}))
    created = manager.recreate(EnvironmentSpec(name="missing", dependencies={"python": "3.11"}))
    assert public.remove_calls == ["incomplete"]
    assert rebuilt.environment.start_count == 1
    assert created.environment.start_count == 1
    assert public.replace_existing == [False, False, False]
    assert manager.get_or_create(EnvironmentSpec(name="other", dependencies={"python": "3.11"})) is other
    assert other.close_count == 0
