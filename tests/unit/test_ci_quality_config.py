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
FAST_TEST_SELECTOR = (
    "not slow and not acceptance and not packaging and not package_tools and not complete "
    "and not wetlands and not public_data and not external_binary and not sairpico_binary "
    "and not model_runtime"
)
FAST_TEST_COMMAND = f'uv run pytest tests -m "{FAST_TEST_SELECTOR}"'
ACCEPTANCE_TEST_SELECTOR = "acceptance and not complete"
ACCEPTANCE_TEST_COMMAND = f'uv run pytest -m "{ACCEPTANCE_TEST_SELECTOR}"'
PACKAGE_TOOLS_TEST_SELECTOR = "package_tools and not complete"
PACKAGE_TOOLS_TEST_COMMAND = f'uv run pytest -m "{PACKAGE_TOOLS_TEST_SELECTOR}"'


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


def test_pytest_registers_non_fast_deterministic_markers() -> None:
    marker_names = {
        marker.split(":", 1)[0]
        for marker in _pyproject()["tool"]["pytest"]["ini_options"]["markers"]
    }

    assert {"acceptance", "packaging", "shared_memory", "slow"} <= marker_names


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
        FAST_TEST_COMMAND,
        "uv run pytest tests/unit/test_package_artifacts.py",
        ACCEPTANCE_TEST_COMMAND,
        PACKAGE_TOOLS_TEST_COMMAND,
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
        assert job["script"] == [FAST_TEST_COMMAND]


def test_gitlab_ci_fast_matrix_is_path_scoped_without_changing_other_test_tiers() -> None:
    ci_config = _gitlab_ci()

    fast_matrix_scripts = [
        ci_config[f"tests:python{version}"]["script"][0]
        for version in ["3.10", "3.11", "3.12"]
    ]

    assert fast_matrix_scripts == [FAST_TEST_COMMAND] * 3
    assert ci_config["tests:acceptance"]["script"] == [ACCEPTANCE_TEST_COMMAND]
    assert ci_config["tests:package-tools"]["script"] == [PACKAGE_TOOLS_TEST_COMMAND]
    assert ci_config["package-artifacts"]["script"] == [
        "uv run pytest tests/unit/test_package_artifacts.py"
    ]


def test_gitlab_ci_has_deterministic_acceptance_job() -> None:
    ci_config = _gitlab_ci()
    job = ci_config["tests:acceptance"]

    assert job["stage"] == "test"
    assert job["script"] == [ACCEPTANCE_TEST_COMMAND]


def test_gitlab_ci_has_deterministic_package_tools_job() -> None:
    ci_config = _gitlab_ci()
    job = ci_config["tests:package-tools"]

    assert job["stage"] == "test"
    assert job["script"] == [PACKAGE_TOOLS_TEST_COMMAND]


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
    tier_docs = [
        ROOT / "README.md",
        ROOT / "docs/source/reference/testing.md",
        ROOT / "docs/source/reference/tool_packages.md",
        ROOT / "docs/source/tutorials/custom_tool_package.rst",
    ]

    required_docs_commands = [
        "uv run ruff check .",
        "uv run pyright",
        FAST_TEST_COMMAND,
        "uv run pytest tests/unit/test_package_artifacts.py",
        ACCEPTANCE_TEST_COMMAND,
        PACKAGE_TOOLS_TEST_COMMAND,
        "uv build --all-packages --out-dir dist/packages",
        "uv run sphinx-build -W --keep-going docs/source docs/_build/html",
    ]

    docs_text = (ROOT / "docs/source/reference/testing.md").read_text()
    for command in [
        FAST_TEST_COMMAND,
        ACCEPTANCE_TEST_COMMAND,
        PACKAGE_TOOLS_TEST_COMMAND,
        "uv run pytest tests/unit/test_package_artifacts.py",
    ]:
        assert command in (ROOT / "README.md").read_text()

    for command in required_docs_commands:
        assert command in docs_text

    for doc_path in tier_docs:
        doc_text = doc_path.read_text()
        if "not slow and not acceptance" in doc_text:
            assert FAST_TEST_COMMAND in doc_text
            assert f'uv run pytest -m "{FAST_TEST_SELECTOR}"' not in doc_text
        if "package_tools" in doc_text and "uv run pytest -m package_tools" in doc_text:
            raise AssertionError(f"{doc_path.relative_to(ROOT)} uses stale package_tools selector")

    assert "Python 3.10, 3.11, and 3.12" in docs_text
    assert (
        f'uv run pytest tests -m "{FAST_TEST_SELECTOR} and not shared_memory"'
        in docs_text
    )
    assert "restricted sandboxes" in docs_text
    assert 'uv run pytest -m "complete and wetlands" --run-complete -rsx' in docs_text
    assert "open build/html/index.html" in (ROOT / "README.md").read_text()


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


def test_deterministic_non_fast_markers_are_not_external_resource_markers() -> None:
    namespace: dict[str, object] = {}
    exec((ROOT / "conftest.py").read_text(), namespace)

    external_marker_names = namespace["EXTERNAL_TEST_MARKER_NAMES"]

    assert not {"acceptance", "package_tools", "packaging", "shared_memory", "slow"}.intersection(
        external_marker_names
    )


def test_shared_memory_marker_is_required_in_fast_ci_selector() -> None:
    ci_config = _gitlab_ci()

    for version in ["3.10", "3.11", "3.12"]:
        command = ci_config[f"tests:python{version}"]["script"][0]
        assert "not shared_memory" not in command


def test_package_artifact_tests_are_packaging_marked() -> None:
    module = ast.parse((ROOT / "tests/unit/test_package_artifacts.py").read_text())

    assert "packaging" in _module_pytestmark_names(module)


def test_deterministic_acceptance_test_modules_are_acceptance_marked() -> None:
    expected_modules = [
        "tests/integration/test_end_to_end.py",
        "tests/specialized_tool_workflows/test_example_workflows.py",
    ]

    missing = []
    for relative_path in expected_modules:
        module = ast.parse((ROOT / relative_path).read_text())
        if "acceptance" not in _module_pytestmark_names(module):
            missing.append(relative_path)

    assert missing == []


def test_priority_workflow_execution_tests_are_acceptance_marked() -> None:
    module_path = ROOT / "tests/priority_workflows/test_workflows.py"
    module = ast.parse(module_path.read_text(), filename=str(module_path))

    acceptance_test_names = {
        "test_synthetic_fish_workflow_executes",
        "test_bbbc038_segmentation_benchmark_constructs_and_executes",
        "test_ome_normalization_executes_tiny_fixture",
        "test_cellpose_stardist_workflow_executes_with_fake_model_runtimes",
        "test_parameter_space_workflow_executes_with_fake_atlas_binary",
        "test_sairpico_smoke_workflow_constructs_and_executes_with_fake_binary",
    }
    markers_by_test = {
        statement.name: _pytest_mark_names(statement)
        for statement in module.body
        if isinstance(statement, ast.FunctionDef | ast.AsyncFunctionDef)
    }

    missing = [
        test_name
        for test_name in sorted(acceptance_test_names)
        if "acceptance" not in markers_by_test[test_name]
    ]
    assert missing == []

    assert "acceptance" not in markers_by_test[
        "test_fish_heavy_workflow_constructs_with_package_imports"
    ]
    assert "acceptance" not in markers_by_test[
        "test_cellpose_stardist_workflow_constructs_with_package_imports"
    ]
