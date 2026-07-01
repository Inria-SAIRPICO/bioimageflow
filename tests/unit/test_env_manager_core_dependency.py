from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, cast

import pytest

from bioimageflow.env_manager import (
    WetlandsEnvManager,
    _bioimageflow_core_editable_dependency,
    _local_bioimageflow_core_project,
)
from bioimageflow_core import EnvironmentSpec


def _manager_with_core_dependency(dependency: object) -> WetlandsEnvManager:
    manager = object.__new__(WetlandsEnvManager)
    manager._bioimageflow_core_dependency = dependency
    return manager


class _MutatingWetlandsEnvironment:
    def __init__(self, dependencies: dict[str, Any]) -> None:
        self.dependencies = dependencies
        self.launched = False
        self.launch_count = 0

    def launch(self, **kwargs: Any) -> None:
        self.launched = True
        self.launch_count += 1
        self.dependencies.setdefault("channels", []).append("bioimageit")


class _MutatingWetlandsManager:
    def __init__(self) -> None:
        self.created_dependencies: list[dict[str, Any]] = []

    def create(self, name: str, dependencies: dict[str, Any]) -> _MutatingWetlandsEnvironment:
        self.created_dependencies.append(dependencies)
        return _MutatingWetlandsEnvironment(dependencies)


def _runtime_manager_with_core_dependency(dependency: object) -> WetlandsEnvManager:
    manager = _manager_with_core_dependency(dependency)
    manager._manager = _MutatingWetlandsManager()
    manager._envs = {}
    manager._launch_configs = {}
    manager._worker_file = "worker.py"
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
        "path": "/repo/packages/bioimageflow-core",
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
    assert first.launch_count == 1
    assert env_spec.dependencies == {
        "python": "3.9",
        "conda": ["bioimageit::simglib=0.1.2"],
        "channels": ["conda-forge", "bioimageit"],
    }
    created_dependencies = manager._manager.created_dependencies[0]
    assert created_dependencies["channels"] == [
        "conda-forge",
        "bioimageit",
        "bioimageit",
    ]
    assert len(manager._manager.created_dependencies) == 2


def test_get_or_create_delegates_same_name_validation_to_wetlands() -> None:
    dependency = {
        "name": "bioimageflow-core",
        "path": "/repo/packages/bioimageflow-core",
        "editable": True,
    }

    class _ValidatingWetlandsManager:
        def __init__(self) -> None:
            self.created_dependencies: list[dict[str, Any]] = []
            self.env = _MutatingWetlandsEnvironment({})

        def create(
            self, name: str, dependencies: dict[str, Any]
        ) -> _MutatingWetlandsEnvironment:
            self.created_dependencies.append(dependencies)
            if len(self.created_dependencies) > 1:
                raise RuntimeError("wetlands recipe mismatch")
            return self.env

    manager = _manager_with_core_dependency(dependency)
    manager._manager = _ValidatingWetlandsManager()
    manager._envs = {}
    manager._launch_configs = {}
    manager._worker_file = "worker.py"
    manager._lock = threading.RLock()

    first = EnvironmentSpec(name="shared", dependencies={"pip": ["numpy==2.4.2"]})
    second = EnvironmentSpec(name="shared", dependencies={"pip": ["scipy==1.17.1"]})

    manager.get_or_create(first)

    with pytest.raises(RuntimeError, match="wetlands recipe mismatch"):
        manager.get_or_create(second)

    assert len(manager._manager.created_dependencies) == 2
    assert manager._manager.env.launched is True


@pytest.mark.parametrize(
    "existing_dependency",
    [
        "bioimageflow-core==0.1.4",
        {
            "name": "bioimageflow-core",
            "path": "/repo/packages/bioimageflow-core",
            "editable": True,
        },
    ],
)
def test_augment_dependencies_does_not_duplicate_existing_core_dependency(
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

    augmented = manager._augment_dependencies(dependencies)

    if isinstance(existing_dependency, dict):
        assert augmented.get("pip") == ["numpy==2.4.2"]
        assert augmented.get("local") == [existing_dependency]
    else:
        assert augmented.get("pip") == ["numpy==2.4.2", existing_dependency]
        assert "local" not in augmented
