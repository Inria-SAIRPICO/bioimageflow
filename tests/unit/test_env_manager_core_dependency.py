from __future__ import annotations

from pathlib import Path

import pytest

from bioimageflow.env_manager import (
    WetlandsEnvManager,
    _bioimageflow_core_editable_dependency,
    _local_bioimageflow_core_project,
)


def _manager_with_core_dependency(dependency: object) -> WetlandsEnvManager:
    manager = object.__new__(WetlandsEnvManager)
    manager._bioimageflow_core_dependency = dependency
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

    augmented = manager._augment_dependencies({"python": "3.9", "pip": ["numpy"]})

    assert augmented["pip"] == ["numpy"]
    assert augmented["local"] == [dependency]


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

    dependencies = {"python": "3.9", "pip": ["numpy"]}
    if isinstance(existing_dependency, dict):
        dependencies["local"] = [existing_dependency]
    else:
        dependencies["pip"].append(existing_dependency)

    augmented = manager._augment_dependencies(dependencies)

    if isinstance(existing_dependency, dict):
        assert augmented["pip"] == ["numpy"]
        assert augmented["local"] == [existing_dependency]
    else:
        assert augmented["pip"] == ["numpy", existing_dependency]
        assert "local" not in augmented
