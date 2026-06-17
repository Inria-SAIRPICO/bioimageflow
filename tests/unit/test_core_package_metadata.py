"""Package metadata contract tests for bioimageflow-core."""

from __future__ import annotations

import ast
from pathlib import Path
import sys

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - Python < 3.11
    import tomli as tomllib


ROOT = Path(__file__).parents[2]


def _pyproject(path: Path) -> dict:
    return tomllib.loads(path.read_text())


def _dependency_names(dependencies: list[str]) -> set[str]:
    names = set()
    for dependency in dependencies:
        names.add(_dependency_name(dependency))
    return names


def _dependency_name(dependency: str) -> str:
    name = dependency.split(";", 1)[0].strip()
    for separator in ("<", ">", "=", "!", "~", "["):
        name = name.split(separator, 1)[0].strip()
    return name.lower()


def _package_pyprojects() -> list[Path]:
    return sorted((ROOT / "packages").glob("*/pyproject.toml"))


def _project(path: Path) -> dict:
    return _pyproject(path)["project"]


def _dependency_entries(dependencies: list[str], package_name: str) -> list[str]:
    return [
        dependency
        for dependency in dependencies
        if _dependency_name(dependency) == package_name.lower()
    ]


def _runtime_imports_package(package_dir: Path, import_name: str) -> bool:
    package_names = [
        child.name
        for child in package_dir.iterdir()
        if child.is_dir() and (child / "__init__.py").exists()
    ]
    for package_name in package_names:
        for source in sorted((package_dir / package_name).glob("**/*.py")):
            tree = ast.parse(source.read_text(), filename=str(source))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    if any(alias.name == import_name for alias in node.names):
                        return True
                elif isinstance(node, ast.ImportFrom) and node.module is not None:
                    if node.module == import_name or node.module.startswith(f"{import_name}."):
                        return True
    return False


def test_direct_runtime_imports_are_declared() -> None:
    expected = {
        "bioimageflow": {"numpy"},
        "bioimageflow-spot-tools": {"pandas"},
        "bioimageflow-tracking-tools": {"pandas"},
    }
    offenders: dict[str, set[str]] = {}

    for path in _package_pyprojects():
        project = _project(path)
        dependencies = _dependency_names(project.get("dependencies", []))
        missing = expected.get(project["name"], set()) - dependencies
        if missing:
            offenders[str(path.relative_to(ROOT))] = missing

    assert offenders == {}


def test_core_declares_numpy_runtime_dependency() -> None:
    pyproject = _pyproject(ROOT / "packages" / "bioimageflow-core" / "pyproject.toml")

    dependencies = _dependency_names(pyproject["project"]["dependencies"])

    assert "numpy" in dependencies
    assert "zero dependencies" not in pyproject["project"]["description"].lower()


def test_workspace_core_pin_matches_local_core_version() -> None:
    workspace = _pyproject(ROOT / "pyproject.toml")
    core = _pyproject(ROOT / "packages" / "bioimageflow-core" / "pyproject.toml")

    assert f"bioimageflow-core=={core['project']['version']}" in workspace["project"]["dependencies"]


def test_first_party_package_versions_are_lockstep() -> None:
    orchestrator = _project(ROOT / "packages" / "bioimageflow" / "pyproject.toml")
    expected = orchestrator["version"]

    versions = {
        str(path.relative_to(ROOT)): _project(path)["version"]
        for path in _package_pyprojects()
    }

    assert versions == {path: expected for path in versions}


def test_first_party_packages_target_python310() -> None:
    requires_python = {
        str(path.relative_to(ROOT)): _project(path)["requires-python"]
        for path in _package_pyprojects()
    }

    assert requires_python == {path: ">=3.10" for path in requires_python}


def test_first_party_packages_require_numpy_declaring_core_version() -> None:
    core = _pyproject(ROOT / "packages" / "bioimageflow-core" / "pyproject.toml")
    expected = f"bioimageflow-core>={core['project']['version']}"
    offenders: dict[str, list[str]] = {}

    for path in sorted((ROOT / "packages").glob("*/pyproject.toml")):
        project = _pyproject(path)["project"]
        dependencies = project.get("dependencies", [])
        core_dependencies = [
            dependency for dependency in dependencies
            if dependency.startswith("bioimageflow-core")
        ]
        if project["name"] != "bioimageflow-core" and core_dependencies != [expected]:
            offenders[str(path.relative_to(ROOT))] = core_dependencies

    assert offenders == {}


def test_tool_packages_require_current_orchestrator_version() -> None:
    orchestrator = _project(ROOT / "packages" / "bioimageflow" / "pyproject.toml")
    expected = f"bioimageflow>={orchestrator['version']}"
    offenders: dict[str, list[str]] = {}

    for path in _package_pyprojects():
        project = _project(path)
        dependencies = project.get("dependencies", [])
        orchestrator_dependencies = _dependency_entries(dependencies, "bioimageflow")
        if (
            project["name"] not in {"bioimageflow", "bioimageflow-core"}
            and _runtime_imports_package(path.parent, "bioimageflow")
            and orchestrator_dependencies != [expected]
        ):
            offenders[str(path.relative_to(ROOT))] = orchestrator_dependencies

    assert offenders == {}


def test_publishable_packages_declare_existing_readmes() -> None:
    offenders: dict[str, str | None] = {}

    for path in _package_pyprojects():
        project = _project(path)
        readme = project.get("readme")
        if not isinstance(readme, str) or not (path.parent / readme).is_file():
            offenders[str(path.relative_to(ROOT))] = readme

    assert offenders == {}


def test_docs_python_requirement_matches_v1_contract() -> None:
    docs = [
        ROOT / "README.md",
        ROOT / "docs" / "source" / "installation.rst",
    ]
    offenders = [
        str(path.relative_to(ROOT))
        for path in docs
        if "Python >= 3.13" in path.read_text() or "Python >= 3.10" not in path.read_text()
    ]

    assert offenders == []


def test_docs_release_matches_orchestrator_version() -> None:
    orchestrator = _project(ROOT / "packages" / "bioimageflow" / "pyproject.toml")
    conf = ROOT / "docs" / "source" / "conf.py"

    assert f'release = "{orchestrator["version"]}"' in conf.read_text()


def test_primary_docs_do_not_describe_core_as_zero_dependency() -> None:
    docs = [
        ROOT / "README.md",
        ROOT / "docs" / "source" / "index.rst",
        ROOT / "docs" / "source" / "installation.rst",
        ROOT / "docs" / "source" / "concepts" / "architecture.rst",
        ROOT / "docs" / "source" / "reference" / "api" / "core.rst",
        ROOT / "docs" / "source" / "specs.md",
        ROOT / "packages" / "bioimageflow-core" / "bioimageflow_core" / "io.py",
        ROOT / "packages" / "bioimageflow-core" / "bioimageflow_core" / "shm.py",
        ROOT / "packages" / "bioimageflow-core" / "bioimageflow_core" / "tool.py",
        ROOT / "packages" / "bioimageflow-core" / "bioimageflow_core" / "types.py",
    ]

    offenders = [
        str(path.relative_to(ROOT))
        for path in docs
        if "zero-dependency core" in path.read_text().lower()
        or "zero external dependencies" in path.read_text().lower()
        or "zero deps" in path.read_text().lower()
    ]

    assert offenders == []
