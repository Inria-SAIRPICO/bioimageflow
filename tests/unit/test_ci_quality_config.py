"""Contracts for repository quality tooling and CI configuration."""

from __future__ import annotations

import ast
import json
from pathlib import Path
import sys

import yaml

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - Python < 3.11
    import tomli as tomllib


ROOT = Path(__file__).parents[2]


def _pyproject() -> dict:
    return tomllib.loads((ROOT / "pyproject.toml").read_text())


def _gitlab_ci() -> dict:
    return yaml.safe_load((ROOT / ".gitlab-ci.yml").read_text())


def test_ruff_policy_is_explicit() -> None:
    ruff = _pyproject()["tool"]["ruff"]

    assert ruff["target-version"] == "py310"
    assert {".worktrees", "docs/_build", "dist"} <= set(ruff["extend-exclude"])
    assert _pyproject()["tool"]["ruff"]["lint"]["select"] == ["E4", "E7", "E9", "F"]


def test_pytest_uses_strict_marker_configuration() -> None:
    pytest_config = _pyproject()["tool"]["pytest"]["ini_options"]

    assert "--strict-config" in pytest_config["addopts"]
    assert "--strict-markers" in pytest_config["addopts"]


def test_pyright_is_an_implementation_gate_for_this_phase() -> None:
    pyright = json.loads((ROOT / "pyrightconfig.json").read_text())

    assert pyright["pythonVersion"] == "3.10"
    assert pyright["typeCheckingMode"] == "basic"
    assert pyright["reportMissingTypeStubs"] is False
    assert pyright["include"] == ["packages"]
    assert "**/tests" in pyright["exclude"]


def test_gitlab_ci_runs_documented_deterministic_quality_gates() -> None:
    ci_text = (ROOT / ".gitlab-ci.yml").read_text()

    expected_commands = [
        "uv sync --locked --all-packages --group dev",
        "uv run ruff check .",
        "uv run pyright",
        'uv run pytest -m "not slow"',
        "uv run pytest tests/unit/test_package_artifacts.py",
        "uv build --all-packages --out-dir dist/packages",
        "uv run sphinx-build -W --keep-going docs/source docs/_build/html",
    ]

    for command in expected_commands:
        assert command in ci_text
    assert "ghcr.io/astral-sh/uv:python3.10-bookworm" in ci_text


def test_gitlab_ci_has_supported_python_test_matrix() -> None:
    ci_config = _gitlab_ci()

    for version in ["3.10", "3.11", "3.12"]:
        job = ci_config[f"tests:python{version}"]
        assert job["stage"] == "test"
        assert job["image"] == f"ghcr.io/astral-sh/uv:python{version}-bookworm"
        assert job["script"] == ['uv run pytest -m "not slow"']


def test_gitlab_ci_complete_jobs_are_optional_and_separate() -> None:
    ci_config = _gitlab_ci()
    template = ci_config[".complete-test-job"]

    assert template["stage"] == "complete"
    assert template["allow_failure"] is True
    assert template["rules"] == [
        {"if": '$CI_PIPELINE_SOURCE == "schedule"', "when": "on_success"},
        {"when": "manual"},
    ]
    for job_name, selector in {
        "complete-wetlands": 'complete and wetlands',
        "complete-public-data": 'complete and public_data',
        "complete-external-binaries": 'complete and external_binary',
        "complete-model-runtimes": 'complete and model_runtime',
    }.items():
        job = ci_config[job_name]
        assert job["extends"] == ".complete-test-job"
        assert job["script"] == [f'uv run pytest -m "{selector}" --run-complete -rsx']

    public_data_job = ci_config["complete-public-data"]
    assert public_data_job["variables"] == {"BIOIMAGEFLOW_ALLOW_PUBLIC_DOWNLOADS": "1"}


def test_contributor_docs_match_ci_quality_commands() -> None:
    docs_text = (ROOT / "docs/source/reference/testing.md").read_text()
    readme_text = (ROOT / "README.md").read_text()

    for command in [
        "uv run ruff check .",
        "uv run pyright",
        'uv run pytest -m "not slow"',
        "uv run pytest tests/unit/test_package_artifacts.py",
        "uv build --all-packages --out-dir dist/packages",
        "uv run sphinx-build -W --keep-going docs/source docs/_build/html",
    ]:
        assert command in docs_text

    assert "Python 3.10, 3.11, and 3.12" in docs_text
    assert 'uv run pytest -m "complete and wetlands" --run-complete -rsx' in docs_text
    assert "open build/html/index.html" in readme_text


def test_package_test_modules_are_marked_package_tools() -> None:
    package_test_files = sorted((ROOT / "packages").glob("*/tests/test_*.py"))

    missing = []
    for path in package_test_files:
        module = ast.parse(path.read_text(), filename=str(path))
        module_markers = _module_pytestmark_names(module)
        if "package_tools" not in module_markers:
            missing.append(str(path.relative_to(ROOT)))

    assert missing == []


def _pytest_mark_names(node: ast.AST) -> set[str]:
    names = set()
    for child in ast.walk(node):
        if (
            isinstance(child, ast.Attribute)
            and isinstance(child.value, ast.Attribute)
            and child.value.attr == "mark"
            and isinstance(child.value.value, ast.Name)
            and child.value.value.id == "pytest"
        ):
            names.add(child.attr)
    return names


def _module_pytestmark_names(module: ast.Module) -> set[str]:
    names = set()
    for statement in module.body:
        if isinstance(statement, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "pytestmark"
            for target in statement.targets
        ):
            names.update(_pytest_mark_names(statement.value))
    return names


def test_slow_tests_are_also_external_tier_tests() -> None:
    external_markers = {
        "wetlands",
        "complete",
        "public_data",
        "external_binary",
        "sairpico_binary",
        "model_runtime",
    }
    violations = []

    for path in sorted([*ROOT.glob("tests/**/test_*.py"), *ROOT.glob("packages/*/tests/test_*.py")]):
        module = ast.parse(path.read_text(), filename=str(path))
        module_markers = _module_pytestmark_names(module)
        class_markers: dict[str, set[str]] = {}
        for statement in module.body:
            if isinstance(statement, ast.ClassDef):
                class_markers[statement.name] = set().union(
                    *[_pytest_mark_names(decorator) for decorator in statement.decorator_list],
                    set(),
                )
                functions = [
                    child
                    for child in statement.body
                    if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef)
                    and child.name.startswith("test")
                ]
                for function in functions:
                    markers = set(module_markers)
                    markers.update(class_markers[statement.name])
                    for decorator in function.decorator_list:
                        markers.update(_pytest_mark_names(decorator))
                    if "slow" in markers and not markers.intersection(external_markers):
                        violations.append(f"{path.relative_to(ROOT)}::{statement.name}::{function.name}")
            elif isinstance(statement, ast.FunctionDef | ast.AsyncFunctionDef) and statement.name.startswith("test"):
                markers = set(module_markers)
                for decorator in statement.decorator_list:
                    markers.update(_pytest_mark_names(decorator))
                if "slow" in markers and not markers.intersection(external_markers):
                    violations.append(f"{path.relative_to(ROOT)}::{statement.name}")

    assert violations == []
