"""Worker-free public admission, selected replacement and held-receipt fences."""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from wetlands import RuntimeContentReceipt

from bioimageflow.env_manager import EnvironmentShutdownError
from bioimageflow_core import EnvironmentSpec
from tests.testkit.env_manager_fakes import (
    _GenerationEnvironment, _Operation, _generation_runtime_manager,
)


@pytest.fixture
def runtime(monkeypatch: pytest.MonkeyPatch):
    reads = []

    def receipt(environment):
        reads.append(environment.generation_id)
        return RuntimeContentReceipt(
            generation_id=environment.generation_id,
            recipe_hash=environment.recipe_hash,
            lockfile_hash=environment.lockfile_hash,
            scientific_facts={
                "schema_version": 1,
                "python": {
                    "implementation": "cpython", "version": [3, 10, 16],
                    "cache_tag": "cpython-310", "soabi": "cpython-310-test",
                    "platform": "test", "machine": "test", "executable_digest": "0" * 64,
                },
                "distributions": [], "resolved_artifacts": [], "editable_sources": [],
            },
        )

    monkeypatch.setattr(_GenerationEnvironment, "lockfile_hash", "b" * 64, raising=False)
    monkeypatch.setattr(_GenerationEnvironment, "runtime_content_receipt", receipt, raising=False)
    manager, public = _generation_runtime_manager()
    spec = EnvironmentSpec("segment", {"python": "3.11"})
    return SimpleNamespace(manager=manager, public=public, spec=spec, reads=reads)


def test_missing_runtime_is_observed_once_without_starting_workers(runtime):
    manager, public, spec = runtime.manager, runtime.public, runtime.spec
    preparations, events, admissions = [], [], {}
    receipt = manager.admit_runtime(
        spec, provision=True, admissions=admissions,
        on_preparation=preparations.append, on_provision_event=events.append,
    )
    environment = public.environment(spec.name)
    assert receipt is not None and receipt.generation_id == environment.generation_id
    assert [event.action for event in preparations] == ["creating"]
    assert preparations[0].requested_recipe_hash == receipt.recipe_hash
    assert events == ["pixi installed"]
    assert environment.start_count == 0 and manager.running_environments() == ()
    assert manager.admit_runtime(spec, provision=True, admissions=admissions,
                                 on_preparation=preparations.append) is receipt
    assert len(runtime.reads) == len(preparations) == 1
    assert manager.get_or_create(spec, runtime_receipt=receipt) is environment.pool
    assert environment.start_count == 1


def test_matching_runtime_reuses_content_even_with_stale_replacement_permission(runtime):
    manager, public, spec = runtime.manager, runtime.public, runtime.spec
    environment = public.provision(spec.name, manager._to_wetlands_spec(spec)).wait_for()
    preparations = []
    receipt = manager.admit_runtime(spec, provision=True, replace_stale=True,
                                    on_preparation=preparations.append)
    assert receipt is not None
    assert public.environment(spec.name) is environment
    assert [event.action for event in preparations] == ["reusing"]
    assert public.remove_calls == [] and len(public.provisioned_specs) == 1
    assert environment.start_count == 0


def test_planning_and_unauthorized_stale_admission_have_no_lifecycle_effects(runtime):
    manager, public, spec = runtime.manager, runtime.public, runtime.spec
    assert manager.admit_runtime(spec, provision=False) is None
    old_spec = EnvironmentSpec(spec.name, {"python": "3.10"})
    old = public.provision(spec.name, manager._to_wetlands_spec(old_spec)).wait_for()
    observations = []
    assert manager.admit_runtime(spec, provision=False, on_preparation=observations.append) is None
    with pytest.raises(ValueError, match="different ready recipe"):
        manager.admit_runtime(spec, provision=True, on_preparation=observations.append)
    assert public.environment(spec.name) is old
    assert old.start_count == 0 and public.remove_calls == []
    assert len(public.provisioned_specs) == 1 and observations == []


def test_stale_replacement_drains_only_selected_pool_before_remove_without_new_start(runtime):
    manager, public, spec = runtime.manager, runtime.public, runtime.spec
    old_spec = EnvironmentSpec(spec.name, {"python": "3.10"})
    old_pool = manager.get_or_create(old_spec)
    unrelated = manager.get_or_create(EnvironmentSpec("unrelated", {"python": "3.11"}))
    grants = []
    manager._shared_memory_grants[spec.name] = [SimpleNamespace(drained=lambda: grants.append("selected"))]
    manager._shared_memory_grants["unrelated"] = [SimpleNamespace(drained=lambda: grants.append("unrelated"))]
    preparations, removal, provision = [], [], []
    receipt = manager.admit_runtime(
        spec, provision=True, replace_stale=True,
        on_preparation=preparations.append,
        on_removal_event=removal.append, on_provision_event=provision.append,
    )
    selected = public.environment(spec.name)
    assert receipt is not None and receipt.generation_id == selected.generation_id
    assert selected is not old_pool.environment and selected.start_count == 0
    assert old_pool.close_count == 1 and grants == ["selected"]
    assert manager.running_environments() == ("unrelated",)
    assert unrelated.close_count == 0 and manager._pools["unrelated"] is unrelated
    assert public.remove_calls == [spec.name]
    assert [event.action for event in preparations] == ["updating"]
    assert preparations[0].existing_recipe_hash == old_pool.environment.recipe_hash
    assert removal == provision == ["pixi installed"]


def test_failed_pool_close_keeps_retry_owner_and_refuses_removal(runtime):
    manager, public, spec = runtime.manager, runtime.public, runtime.spec
    pool = manager.get_or_create(EnvironmentSpec(spec.name, {"python": "3.10"}))
    primary = pool.close_failure = RuntimeError("close remains pending")
    admissions = {}
    with pytest.raises(EnvironmentShutdownError) as failure:
        manager.admit_runtime(spec, provision=True, replace_stale=True, admissions=admissions)
    assert failure.value.__cause__ is primary
    assert failure.value.failures == ((spec.name, primary),)
    assert manager._pools[spec.name] is pool and admissions == {}
    assert public.remove_calls == [] and len(public.provisioned_specs) == 1
    pool.close_failure = None
    receipt = manager.admit_runtime(spec, provision=True, replace_stale=True, admissions=admissions)
    assert receipt is not None and pool.close_count == 2
    assert public.environment(spec.name).start_count == 0


def test_preparation_failure_precedes_destructive_effects(runtime):
    manager, public, spec = runtime.manager, runtime.public, runtime.spec
    pool = manager.get_or_create(EnvironmentSpec(spec.name, {"python": "3.10"}))
    primary = RuntimeError("observer refused")

    def refuse(_preparation):
        raise primary

    with pytest.raises(RuntimeError) as failure:
        manager.admit_runtime(spec, provision=True, replace_stale=True, on_preparation=refuse)
    assert failure.value is primary and pool.close_count == 0
    assert manager._pools[spec.name] is pool and public.remove_calls == []
    assert len(public.provisioned_specs) == 1


def test_failed_stale_provision_leaves_no_receipt_and_retry_remains_worker_free(runtime, monkeypatch):
    manager, public, spec = runtime.manager, runtime.public, runtime.spec
    pool = manager.get_or_create(EnvironmentSpec(spec.name, {"python": "3.10"}))
    original = public.provision
    primary = RuntimeError("provision failed")
    admissions = {}

    def fail(*args, **kwargs):
        raise primary

    monkeypatch.setattr(public, "provision", fail)
    with pytest.raises(RuntimeError) as failure:
        manager.admit_runtime(spec, provision=True, replace_stale=True, admissions=admissions)
    assert failure.value is primary and admissions == {}
    assert pool.close_count == 1 and manager.running_environments() == ()
    assert spec.name not in public.environments and public.remove_calls == [spec.name]
    monkeypatch.setattr(public, "provision", original)
    receipt = manager.admit_runtime(spec, provision=True, replace_stale=True, admissions=admissions)
    assert receipt is not None and public.environment(spec.name).start_count == 0
    assert public.remove_calls == [spec.name]


def test_unavailable_current_receipt_uses_observed_worker_free_owned_repair(runtime, monkeypatch):
    from wetlands import RuntimeContentUnavailableError

    manager, public, spec = runtime.manager, runtime.public, runtime.spec
    pool = manager.get_or_create(spec)
    old = public.environment(spec.name)
    original = _GenerationEnvironment.runtime_content_receipt

    def changed(environment):
        if environment is old:
            raise RuntimeContentUnavailableError("changed editable content")
        return original(environment)

    monkeypatch.setattr(_GenerationEnvironment, "runtime_content_receipt", changed)
    preparations = []
    receipt = manager.admit_runtime(spec, provision=True, on_preparation=preparations.append)
    assert receipt is not None and receipt.generation_id != old.generation_id
    assert pool.close_count == 1 and public.remove_calls == [spec.name]
    assert public.environment(spec.name).start_count == 0
    assert [event.action for event in preparations] == ["updating"]


@pytest.mark.parametrize("change", ["generation_id", "lockfile_hash"])
def test_memoized_receipt_refuses_changed_owner_without_replacement(runtime, change):
    manager, public, spec = runtime.manager, runtime.public, runtime.spec
    admissions = {}
    receipt = manager.admit_runtime(spec, provision=True, admissions=admissions)
    setattr(public.environment(spec.name), change, "changed")
    with pytest.raises(RuntimeError, match="changed after runtime admission"):
        manager.admit_runtime(spec, provision=True, replace_stale=True, admissions=admissions)
    assert list(admissions.values()) == [receipt]
    assert public.remove_calls == [] and len(public.provisioned_specs) == 1
    assert len(runtime.reads) == 1 and public.environment(spec.name).start_count == 0


def test_pool_handoff_cannot_force_replace_a_held_receipt(runtime):
    manager, public, spec = runtime.manager, runtime.public, runtime.spec
    receipt = manager.admit_runtime(spec, provision=True)
    with pytest.raises(ValueError, match="cannot be replaced"):
        manager.get_or_create(spec, runtime_receipt=receipt, replace_existing=True)
    assert public.remove_calls == [] and len(public.provisioned_specs) == 1
    assert public.environment(spec.name).start_count == 0


def test_one_operation_cannot_replace_its_held_receipt_through_another_recipe(runtime):
    manager, public, spec = runtime.manager, runtime.public, runtime.spec
    admissions = {}
    receipt = manager.admit_runtime(spec, provision=True, admissions=admissions)
    changed = EnvironmentSpec(spec.name, {"python": "3.10"})
    with pytest.raises(ValueError, match="already admitted a different recipe"):
        manager.admit_runtime(changed, provision=True, replace_stale=True, admissions=admissions)
    assert list(admissions.values()) == [receipt]
    assert public.remove_calls == [] and len(public.provisioned_specs) == 1
    assert public.environment(spec.name).start_count == 0


@pytest.mark.parametrize("kwargs", [
    {"provision": 1}, {"provision": True, "replace_stale": "yes"},
    {"provision": False, "replace_stale": True},
    {"provision": True, "on_preparation": 1},
    {"provision": True, "on_provision_event": 1},
    {"provision": True, "on_removal_event": 1},
    {"provision": True, "admissions": []},
])
def test_invalid_arguments_refuse_before_any_preparation(runtime, monkeypatch, kwargs):
    monkeypatch.setattr(runtime.public, "environment", lambda name: pytest.fail("provider touched"))
    with pytest.raises((TypeError, ValueError)):
        runtime.manager.admit_runtime(runtime.spec, **kwargs)
    assert runtime.public.provisioned_specs == [] and runtime.public.remove_calls == []


@pytest.mark.parametrize("phase", ["removal", "provision"])
def test_listener_interruption_drains_exact_operation_and_never_starts_next_phase(
    runtime, monkeypatch, phase,
):
    manager, public, spec = runtime.manager, runtime.public, runtime.spec
    public.provision(spec.name, manager._to_wetlands_spec(
        EnvironmentSpec(spec.name, {"python": "3.10"}))).wait_for()
    primary = KeyboardInterrupt("listener interrupted")
    operations = []

    class InterruptedOperation(_Operation):
        def listen(self, callback: Any) -> None:
            self.callback = callback
            callback("replayed event")

        def cancel(self) -> bool:
            assert not hasattr(self, "callback")
            self.cancelled = True
            return True

        def wait_for_completion(self, timeout=None) -> None:
            assert timeout is None
            self.completed = True

    original = public.remove if phase == "removal" else public.provision

    def operation(*args, **kwargs):
        result = InterruptedOperation(original(*args, **kwargs).value)
        operations.append(result)
        return result

    monkeypatch.setattr(public, "remove" if phase == "removal" else "provision", operation)

    def interrupted(_event):
        raise primary

    callbacks = {"on_removal_event" if phase == "removal" else "on_provision_event": interrupted}
    admissions = {}
    with pytest.raises(KeyboardInterrupt) as failure:
        manager.admit_runtime(spec, provision=True, replace_stale=True,
                              admissions=admissions, **callbacks)
    assert failure.value is primary and admissions == {}
    assert len(operations) == 1 and operations[0].cancelled and operations[0].completed
    assert manager.running_environments() == () and public.remove_calls == [spec.name]
    assert len(public.provisioned_specs) == (1 if phase == "removal" else 2)
    if phase == "provision":
        assert public.environment(spec.name).start_count == 0
