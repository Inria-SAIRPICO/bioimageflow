"""Tests for the advisory affected-test helper."""

from tests.support.affected_tests import ADVISORY_NOTE, commands_for_paths, main
from tests.support.ci_selectors import (
    ACCEPTANCE_TEST_COMMAND,
    CI_QUALITY_CONFIG_COMMAND,
    DEFAULT_TEST_COMMAND,
    DOCS_BUILD_COMMAND,
    FAST_TEST_COMMAND,
    PACKAGE_METADATA_CONTRACTS_COMMAND,
    PACKAGE_TOOLS_TEST_COMMAND,
)


def test_package_changes_run_package_local_tests_and_metadata_contracts() -> None:
    assert commands_for_paths(["packages/bioimageflow-spot-tools/bioimageflow_spot_tools/detection.py"]) == [
        'uv run pytest packages/bioimageflow-spot-tools/tests -m "not complete"',
        PACKAGE_METADATA_CONTRACTS_COMMAND,
    ]


def test_core_package_source_changes_run_fast_tests() -> None:
    assert commands_for_paths(["packages/bioimageflow/bioimageflow/workflow.py"]) == [
        FAST_TEST_COMMAND
    ]
    assert commands_for_paths(["packages/bioimageflow-core/bioimageflow_core/types.py"]) == [
        FAST_TEST_COMMAND
    ]


def test_test_file_changes_run_that_test_file() -> None:
    assert commands_for_paths(["tests/unit/test_storage.py"]) == [
        "uv run pytest tests/unit/test_storage.py",
        FAST_TEST_COMMAND,
    ]


def test_non_fast_test_file_changes_also_run_fast_selector() -> None:
    assert commands_for_paths(["tests/specialized_tool_workflows/test_example_workflows.py"]) == [
        "uv run pytest tests/specialized_tool_workflows/test_example_workflows.py",
        FAST_TEST_COMMAND,
    ]


def test_docs_and_ci_config_changes_run_quality_contracts_and_docs_build() -> None:
    assert commands_for_paths(["docs/source/reference/testing.md", ".gitlab-ci.yml"]) == [
        CI_QUALITY_CONFIG_COMMAND,
        DOCS_BUILD_COMMAND,
    ]


def test_example_workflow_changes_run_acceptance_and_package_tools() -> None:
    assert commands_for_paths(["example-workflows/fish_analysis/workflow.py"]) == [
        ACCEPTANCE_TEST_COMMAND,
        PACKAGE_TOOLS_TEST_COMMAND,
    ]


def test_unknown_paths_fail_open_to_default_pytest() -> None:
    assert commands_for_paths(["unexpected/location.txt"]) == [DEFAULT_TEST_COMMAND]


def test_core_package_metadata_changes_do_not_use_missing_package_local_tests() -> None:
    assert commands_for_paths(["packages/bioimageflow-core/pyproject.toml"]) == [
        FAST_TEST_COMMAND
    ]


def test_empty_path_list_fails_open_to_default_pytest() -> None:
    assert commands_for_paths([]) == [DEFAULT_TEST_COMMAND]


def test_cli_prints_advisory_note_and_commands(capsys) -> None:
    exit_code = main(["docs/source/reference/testing.md"])

    assert exit_code == 0
    assert capsys.readouterr().out.splitlines() == [
        ADVISORY_NOTE,
        CI_QUALITY_CONFIG_COMMAND,
        DOCS_BUILD_COMMAND,
    ]
