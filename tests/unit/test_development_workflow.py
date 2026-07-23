"""Contract tests for the focused development workflow."""

from pathlib import Path

from scripts.check_file_sizes import violations as file_size_violations
from scripts.check_import_boundaries import violations as import_boundary_violations
from tests.support.affected_tests import (
    commands_for_paths,
    load_ownership,
    unowned_platform_paths,
)
from tests.support.ci_selectors import (
    DIRECT_INTEGRATION_TEST_COMMAND,
    FAST_TEST_COMMAND,
    UNIT_TEST_COMMAND,
)


ROOT = Path(__file__).parents[2]


def test_orchestrator_and_test_modules_respect_size_limits() -> None:
    assert file_size_violations(ROOT) == []


def test_platform_import_boundaries_are_acyclic() -> None:
    assert import_boundary_violations(ROOT) == []


def test_every_orchestrator_module_has_affected_test_ownership() -> None:
    assert unowned_platform_paths(root=ROOT) == []


def test_ownership_map_references_existing_tests() -> None:
    for area in load_ownership(ROOT / "tests" / "ownership.toml"):
        for source_path in area["sources"]:
            assert (ROOT / source_path).exists(), f"{area['name']}: missing {source_path}"
        for test_path in area["edit_tests"]:
            assert (ROOT / test_path).exists(), f"{area['name']}: missing {test_path}"


def test_edit_stage_selects_focused_engine_tests() -> None:
    commands = commands_for_paths(
        ["packages/bioimageflow/bioimageflow/engine/scheduler.py"],
        stage="edit",
        root=ROOT,
    )
    assert len(commands) == 1
    assert "tests/integration/test_engine_injection.py" in commands[0]
    assert commands[0] != FAST_TEST_COMMAND


def test_precommit_stage_selects_independent_suites() -> None:
    commands = commands_for_paths(
        ["packages/bioimageflow/bioimageflow/storage/manifests.py"],
        stage="precommit",
        root=ROOT,
    )
    assert commands == [UNIT_TEST_COMMAND, DIRECT_INTEGRATION_TEST_COMMAND]


def test_merge_and_unknown_paths_fail_open_to_fast_suite() -> None:
    assert commands_for_paths(["unknown.file"], root=ROOT) == [FAST_TEST_COMMAND]
    assert commands_for_paths(
        ["packages/bioimageflow/bioimageflow/cache/identity.py"],
        stage="merge",
        root=ROOT,
    ) == [FAST_TEST_COMMAND]
