"""Main-process requirements declared by the nine first-party tool packages."""

from __future__ import annotations

import sys
from pathlib import Path

from packaging.requirements import Requirement

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - Python < 3.11
    import tomli as tomllib


ROOT = Path(__file__).parents[2]
EXPECTED = {
    "common": {"bioimageflow-core", "bioimageflow", "pandas"},
    "io": {"bioimageflow-core"},
    "measurement": {"bioimageflow-core", "bioimageflow", "numpy", "pandas"},
    "phasor": {"bioimageflow-core"},
    "restoration": {"bioimageflow-core"},
    "sairpico": {"bioimageflow-core"},
    "segmentation": {"bioimageflow-core"},
    "spot": {"bioimageflow-core", "bioimageflow", "imageio", "numpy", "pandas", "scipy"},
    "tracking": {"bioimageflow-core", "bioimageflow", "numpy", "pandas", "scipy"},
}


def test_tool_package_runtime_dependency_matrix() -> None:
    for domain, expected in EXPECTED.items():
        project_path = ROOT / "packages" / f"bioimageflow-{domain}-tools" / "pyproject.toml"
        project = tomllib.loads(project_path.read_text())["project"]
        actual = {Requirement(value).name.lower() for value in project["dependencies"]}
        assert actual == expected, project_path
        requirements = {Requirement(value).name: Requirement(value) for value in project["dependencies"]}
        core = requirements["bioimageflow-core"].specifier
        assert set((item.operator, item.version) for item in core) == {(">=", "0.5.0"), ("<", "0.6")}
        if "bioimageflow" in requirements:
            orchestrator = requirements["bioimageflow"].specifier
            assert set((item.operator, item.version) for item in orchestrator) == {(">=", "0.9.0"), ("<", "1")}
