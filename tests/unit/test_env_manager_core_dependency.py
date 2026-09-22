from __future__ import annotations

import importlib.metadata
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from bioimageflow.env_manager import (
    WetlandsEnvManager,
    _bioimageflow_core_editable_dependency,
    _bioimageflow_core_pin,
    _local_bioimageflow_core_project,
)
from bioimageflow_core import EnvironmentSpec


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


class _MutatingWetlandsManager:
    def __init__(self) -> None:
        self.provisioned_specs: list[Any] = []
        self.replace_existing: list[bool] = []
        self.environment = _MutatingWetlandsEnvironment()
        self.infos: tuple[Any, ...] = ()

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
        return _Operation(self.environment)


def _runtime_manager_with_core_dependency(dependency: object) -> WetlandsEnvManager:
    manager = _manager_with_core_dependency(dependency)
    manager._manager = _MutatingWetlandsManager()
    manager._environments = {}
    manager._pools = {}
    manager._pool_configs = {}
    manager._specs = {}
    manager._lock = threading.RLock()
    return manager


def test_local_bioimageflow_core_project_is_detected_in_source_checkout() -> None:
    project_dir = _local_bioimageflow_core_project()

    assert project_dir is not None
    assert project_dir.name == "bioimageflow-core"
    assert (project_dir / "pyproject.toml").exists()


def test_editable_core_dependency_uses_wetlands_local_package_shape() -> None:
    project_dir = Path("/repo/packages/bioimageflow-core")

    dependency = _bioimageflow_core_editable_dependency(project_dir)

    assert dependency == {
        "name": "bioimageflow-core",
        "path": str(project_dir),
        "editable": True,
    }


def test_manager_defaults_to_pinned_core_dependency(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BIOIMAGEFLOW_USE_LOCAL_CORE", raising=False)

    manager = WetlandsEnvManager()

    assert manager._bioimageflow_core_dependency == _bioimageflow_core_pin()


@pytest.mark.parametrize("value", ["", "0", "false", "no", "off"])
def test_manager_uses_pinned_core_when_env_var_is_false_like(
    monkeypatch: pytest.MonkeyPatch,
    value: str,
) -> None:
    monkeypatch.setenv("BIOIMAGEFLOW_USE_LOCAL_CORE", value)

    manager = WetlandsEnvManager()

    assert manager._bioimageflow_core_dependency == _bioimageflow_core_pin()


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on"])
def test_manager_uses_local_core_when_env_var_is_truthy(
    monkeypatch: pytest.MonkeyPatch,
    value: str,
) -> None:
    monkeypatch.setenv("BIOIMAGEFLOW_USE_LOCAL_CORE", value)

    manager = WetlandsEnvManager()

    assert manager._bioimageflow_core_dependency == _bioimageflow_core_editable_dependency(
        _local_bioimageflow_core_project()
    )


def test_explicit_false_overrides_truthy_local_core_env_var(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BIOIMAGEFLOW_USE_LOCAL_CORE", "1")

    manager = WetlandsEnvManager(use_local_bioimageflow_core=False)

    assert manager._bioimageflow_core_dependency == _bioimageflow_core_pin()


def test_explicit_true_overrides_false_like_local_core_env_var(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BIOIMAGEFLOW_USE_LOCAL_CORE", "0")

    manager = WetlandsEnvManager(use_local_bioimageflow_core=True)

    assert manager._bioimageflow_core_dependency == _bioimageflow_core_editable_dependency(
        _local_bioimageflow_core_project()
    )


def test_explicit_core_dependency_overrides_local_core_env_var(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BIOIMAGEFLOW_USE_LOCAL_CORE", "1")
    dependency = "bioimageflow-core==9.9.9"

    manager = WetlandsEnvManager(bioimageflow_core_dependency=dependency)

    assert manager._bioimageflow_core_dependency == dependency


def test_pinned_core_dependency_uses_installed_distribution_version() -> None:
    assert _bioimageflow_core_pin() == (
        f"bioimageflow-core=={importlib.metadata.version('bioimageflow-core')}"
    )


def test_augment_dependencies_injects_configured_local_core_dependency() -> None:
    dependency = {
        "name": "bioimageflow-core",
        "path": "/repo/packages/bioimageflow-core",
        "editable": True,
    }
    manager = _manager_with_core_dependency(dependency)
    dependencies = {"python": "3.9", "pip": ["numpy==2.4.2"]}

    augmented = manager._augment_dependencies(dependencies)

    assert augmented.get("pip") == ["numpy==2.4.2"]
    assert augmented.get("local") == [dependency]
    assert "local" not in dependencies


def test_augment_dependencies_does_not_mutate_dependency_spec() -> None:
    dependency = {
        "name": "bioimageflow-core",
        "path": "/repo/packages/bioimageflow-core",
        "editable": True,
    }
    manager = _manager_with_core_dependency(dependency)
    dependencies = {
        "python": "3.9",
        "channels": ["conda-forge", "bioimageit"],
        "pip": ["numpy==2.4.2"],
    }

    augmented = manager._augment_dependencies(dependencies)
    augmented_dict = cast(dict[str, Any], augmented)
    augmented_dict["channels"].append("extra")
    augmented_dict["pip"].append("scipy==1.17.1")
    augmented_dict["local"].append({"name": "other", "path": "/repo/other"})

    assert dependencies == {
        "python": "3.9",
        "channels": ["conda-forge", "bioimageit"],
        "pip": ["numpy==2.4.2"],
    }


def test_get_or_create_ignores_mutations_to_created_dependency_copy() -> None:
    dependency = {
        "name": "bioimageflow-core",
        "path": str(_local_bioimageflow_core_project()),
        "editable": True,
    }
    manager = _runtime_manager_with_core_dependency(dependency)
    env_spec = EnvironmentSpec(
        name="simglib",
        dependencies={
            "python": "3.9",
            "conda": ["bioimageit::simglib=0.1.2"],
            "channels": ["conda-forge", "bioimageit"],
        },
    )

    first = manager.get_or_create(env_spec)
    second = manager.get_or_create(env_spec)

    assert first is second
    assert manager._manager.environment.start_count == 1
    assert env_spec.dependencies == {
        "python": "3.9",
        "conda": ["bioimageit::simglib=0.1.2"],
        "channels": ["conda-forge", "bioimageit"],
    }
    translated = manager._manager.provisioned_specs[0]
    assert translated.channels == ("conda-forge", "bioimageit")
    assert translated.conda == ("simglib=0.1.2",)
    assert len(manager._manager.provisioned_specs) == 1


def test_wetlands_v2_translates_local_pypi_reference_to_typed_package() -> None:
    project = _local_bioimageflow_core_project()
    assert project is not None
    manager = _manager_with_core_dependency(
        f"bioimageflow-core @ {project.as_uri()}"
    )
    spec = manager._to_wetlands_spec(
        EnvironmentSpec(
            name="local-core",
            dependencies={
                "python": "3.13",
                "pip": [f"bioimageflow-core @ {project.as_uri()}"],
            },
        )
    )

    assert spec.python == "3.13.*"
    assert spec.pypi == ()
    assert len(spec.local) == 1
    assert spec.local[0].source == project.resolve()
    assert spec.local[0].distribution_name == "bioimageflow-core"


def test_get_or_create_delegates_same_name_validation_to_wetlands() -> None:
    dependency = {
        "name": "bioimageflow-core",
        "path": str(_local_bioimageflow_core_project()),
        "editable": True,
    }

    class _ValidatingWetlandsManager:
        def __init__(self) -> None:
            self.provisioned_specs: list[Any] = []
            self.env = _MutatingWetlandsEnvironment()

        def managed_environments(self) -> tuple[Any, ...]:
            return ()

        def provision(
            self, name: str, spec: Any, *, replace_existing: bool = False
        ) -> _Operation:
            _ = name, replace_existing
            self.provisioned_specs.append(spec)
            if len(self.provisioned_specs) > 1:
                raise RuntimeError("wetlands recipe mismatch")
            return _Operation(self.env)

    manager = _manager_with_core_dependency(dependency)
    manager._manager = _ValidatingWetlandsManager()
    manager._environments = {}
    manager._pools = {}
    manager._pool_configs = {}
    manager._specs = {}
    manager._lock = threading.RLock()

    first = EnvironmentSpec(name="shared", dependencies={"pip": ["numpy==2.4.2"]})
    second = EnvironmentSpec(name="shared", dependencies={"pip": ["scipy==1.17.1"]})

    manager.get_or_create(first)

    with pytest.raises(ValueError, match="different recipe"):
        manager.get_or_create(second)

    assert len(manager._manager.provisioned_specs) == 1
    assert manager._manager.env.start_count == 1


def test_augment_dependencies_replaces_compatible_core_constraint_with_authoritative_pin() -> None:
    manager = _manager_with_core_dependency("bioimageflow-core==0.4.1")
    dependencies = {
        "python": "3.9",
        "pip": ["numpy==2.4.2", "bioimageflow-core>=0.4,<0.5"],
    }

    augmented = manager._augment_dependencies(dependencies)

    assert augmented["pip"] == ["numpy==2.4.2", "bioimageflow-core==0.4.1"]


@pytest.mark.parametrize(
    "existing_dependency",
    [
        "bioimageflow-core==0.1.4",
        {
            "name": "bioimageflow-core",
            "path": "/repo/other-bioimageflow-core",
            "editable": True,
        },
    ],
)
def test_augment_dependencies_rejects_divergent_core_dependency(
    existing_dependency: object,
) -> None:
    manager = _manager_with_core_dependency(
        {
            "name": "bioimageflow-core",
            "path": "/repo/packages/bioimageflow-core",
            "editable": True,
        }
    )

    dependencies = {"python": "3.9", "pip": ["numpy==2.4.2"]}
    if isinstance(existing_dependency, dict):
        dependencies["local"] = [existing_dependency]
    else:
        dependencies["pip"].append(existing_dependency)

    with pytest.raises(ValueError, match="bioimageflow-core"):
        manager._augment_dependencies(dependencies)


def test_get_or_create_forwards_provision_events_before_wait() -> None:
    manager = _runtime_manager_with_core_dependency("bioimageflow-core==0.4.1")
    spec = EnvironmentSpec(name="segment", dependencies={"python": "3.11"})
    events: list[str] = []

    manager.get_or_create(spec, on_provision_event=events.append)
    manager.get_or_create(spec, on_provision_event=events.append)

    assert events == ["pixi installed"]


def test_get_or_create_replaces_changed_cached_recipe_after_closing_pool() -> None:
    manager = _runtime_manager_with_core_dependency("bioimageflow-core==0.4.1")
    first = EnvironmentSpec(name="segment", dependencies={"python": "3.11"})
    second = EnvironmentSpec(name="segment", dependencies={"python": "3.12"})
    preparations: list[str] = []

    old_pool = manager.get_or_create(first)
    new_pool = manager.get_or_create(
        second,
        replace_existing=True,
        on_preparation=lambda event: preparations.append(event.action),
    )

    assert old_pool.close_count == 1
    assert new_pool is old_pool
    assert manager._manager.replace_existing == [False, True]
    assert preparations == ["updating"]


def test_get_or_create_explicitly_recreates_current_cached_recipe() -> None:
    manager = _runtime_manager_with_core_dependency("bioimageflow-core==0.4.1")
    spec = EnvironmentSpec(name="segment", dependencies={"python": "3.11"})
    preparations: list[str] = []

    old_pool = manager.get_or_create(spec)
    manager.get_or_create(
        spec,
        replace_existing=True,
        on_preparation=lambda event: preparations.append(event.action),
    )

    assert old_pool.close_count == 1
    assert manager._manager.replace_existing == [False, True]
    assert preparations == ["updating"]


def test_inspect_environment_reports_current_and_stale_managed_recipes() -> None:
    from bioimageflow.env_manager import EnvironmentRecipeState

    manager = _runtime_manager_with_core_dependency("bioimageflow-core==0.4.1")
    first = EnvironmentSpec(name="segment", dependencies={"python": "3.11"})
    second = EnvironmentSpec(name="segment", dependencies={"python": "3.12"})

    assert manager.inspect_environment(first) is EnvironmentRecipeState.MISSING
    manager.get_or_create(first)
    manager.stop(first.name)

    assert manager.inspect_environment(first) is EnvironmentRecipeState.CURRENT
    assert manager.inspect_environment(second) is EnvironmentRecipeState.STALE
