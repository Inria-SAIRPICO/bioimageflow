"""Package metadata contract tests for bioimageflow-core."""

from __future__ import annotations

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
        name = dependency.split(";", 1)[0].strip()
        for separator in ("<", ">", "=", "!", "~", "["):
            name = name.split(separator, 1)[0].strip()
        names.add(name.lower())
    return names


def test_core_declares_numpy_runtime_dependency() -> None:
    pyproject = _pyproject(ROOT / "packages" / "bioimageflow-core" / "pyproject.toml")

    dependencies = _dependency_names(pyproject["project"]["dependencies"])

    assert "numpy" in dependencies
    assert "zero dependencies" not in pyproject["project"]["description"].lower()


def test_workspace_core_pin_matches_local_core_version() -> None:
    workspace = _pyproject(ROOT / "pyproject.toml")
    core = _pyproject(ROOT / "packages" / "bioimageflow-core" / "pyproject.toml")

    assert f"bioimageflow-core=={core['project']['version']}" in workspace["project"]["dependencies"]


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
