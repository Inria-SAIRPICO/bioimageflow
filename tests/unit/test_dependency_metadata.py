"""Dependency lower-bound metadata contracts."""

from pathlib import Path
import sys

from packaging.requirements import Requirement

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - Python < 3.11
    import tomli as tomllib


ROOT = Path(__file__).parents[2]


def _pyproject(path: Path) -> dict:
    return tomllib.loads(path.read_text())


def _package_pyprojects() -> list[Path]:
    return sorted((ROOT / "packages").glob("*/pyproject.toml"))


def _has_lower_bound(requirement: Requirement) -> bool:
    return any(
        specifier.operator in {">", ">="}
        for specifier in requirement.specifier
    )


def test_all_third_party_package_dependencies_declare_lower_bounds() -> None:
    first_party = {
        _pyproject(path)["project"]["name"]
        for path in _package_pyprojects()
    }
    offenders: dict[str, list[str]] = {}

    for path in _package_pyprojects():
        dependencies = _pyproject(path)["project"].get("dependencies", [])
        unbounded = [
            dependency
            for dependency in dependencies
            if Requirement(dependency).name not in first_party
            and not _has_lower_bound(Requirement(dependency))
        ]
        if unbounded:
            offenders[str(path.relative_to(ROOT))] = unbounded

    assert offenders == {}


def test_workspace_development_dependencies_declare_lower_bounds() -> None:
    workspace = _pyproject(ROOT / "pyproject.toml")
    dependencies = workspace["dependency-groups"]["dev"]

    assert [
        dependency
        for dependency in dependencies
        if not _has_lower_bound(Requirement(dependency))
    ] == []
