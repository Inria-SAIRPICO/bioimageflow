from __future__ import annotations

from pathlib import Path

import pytest

from bioimageflow.env_manager import (
    WetlandsEnvManager,
    _bioimageflow_core_editable_dependency,
    _bioimageflow_core_pin,
)
from bioimageflow._core_dependency import _configured_core_dependency


@pytest.fixture(autouse=True)
def _clear_core_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BIOIMAGEFLOW_CORE_SOURCE", raising=False)
    monkeypatch.delenv("BIOIMAGEFLOW_USE_LOCAL_CORE", raising=False)


def _core_project(
    root: Path,
    *,
    name: str = "bioimageflow-core",
    version: str = "0.4.1",
    with_package: bool = True,
) -> Path:
    root.mkdir()
    (root / "pyproject.toml").write_text(
        f'[project]\nname = "{name}"\nversion = "{version}"\n',
        encoding="utf-8",
    )
    if with_package:
        (root / "bioimageflow_core").mkdir()
    return root


def test_manager_uses_explicit_core_source_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _core_project(tmp_path / "core")
    monkeypatch.setenv("BIOIMAGEFLOW_CORE_SOURCE", str(project))
    monkeypatch.setenv("BIOIMAGEFLOW_USE_LOCAL_CORE", "0")

    manager = WetlandsEnvManager()

    assert manager._bioimageflow_core_dependency == (
        _bioimageflow_core_editable_dependency(project.resolve())
    )
    assert _configured_core_dependency() == (
        _bioimageflow_core_editable_dependency(project.resolve())
    )


def test_manager_accepts_explicit_core_source_argument(tmp_path: Path) -> None:
    project = _core_project(tmp_path / "core")

    manager = WetlandsEnvManager(bioimageflow_core_source=project)

    assert manager._bioimageflow_core_dependency == (
        _bioimageflow_core_editable_dependency(project.resolve())
    )


def test_explicit_false_disables_core_source_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _core_project(tmp_path / "core")
    monkeypatch.setenv("BIOIMAGEFLOW_CORE_SOURCE", str(project))

    manager = WetlandsEnvManager(use_local_bioimageflow_core=False)

    assert manager._bioimageflow_core_dependency == _bioimageflow_core_pin()


def test_explicit_false_rejects_explicit_core_source_argument(tmp_path: Path) -> None:
    project = _core_project(tmp_path / "core")

    with pytest.raises(ValueError, match="cannot be combined"):
        WetlandsEnvManager(
            bioimageflow_core_source=project,
            use_local_bioimageflow_core=False,
        )


def test_explicit_dependency_ignores_invalid_core_source_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BIOIMAGEFLOW_CORE_SOURCE", str(tmp_path / "missing"))

    manager = WetlandsEnvManager(
        bioimageflow_core_dependency="bioimageflow-core==9.9.9"
    )

    assert manager._bioimageflow_core_dependency == "bioimageflow-core==9.9.9"


@pytest.mark.parametrize(
    ("project_kind", "message"),
    [
        ("missing", "not an existing directory"),
        ("no_pyproject", "has no pyproject.toml"),
        ("wrong_name", "project name is not 'bioimageflow-core'"),
        ("invalid_version", "project version 'not-a-version' is invalid"),
        ("no_package", "has no bioimageflow_core package"),
    ],
)
def test_invalid_core_source_environment_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    project_kind: str,
    message: str,
) -> None:
    project = tmp_path / "core"
    if project_kind == "no_pyproject":
        project.mkdir()
    elif project_kind == "wrong_name":
        _core_project(project, name="not-core")
    elif project_kind == "invalid_version":
        _core_project(project, version="not-a-version")
    elif project_kind == "no_package":
        _core_project(project, with_package=False)
    monkeypatch.setenv("BIOIMAGEFLOW_CORE_SOURCE", str(project))

    with pytest.raises(RuntimeError, match=message):
        WetlandsEnvManager()
