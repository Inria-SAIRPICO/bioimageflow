"""Contract tests for the focused development workflow."""

from pathlib import Path

from scripts.check_file_sizes import violations as file_size_violations
from scripts.check_import_boundaries import violations as import_boundary_violations
from tests.support.affected_tests import (
    commands_for_paths,
    load_ownership,
    nonuniquely_owned_platform_paths,
    unowned_platform_paths,
)
from tests.support.ci_selectors import (
    DIRECT_INTEGRATION_TEST_COMMAND,
    FAST_TEST_COMMAND,
    PARSL_FAST_TEST_COMMAND,
    UNIT_TEST_COMMAND,
    DOCS_BUILD_COMMAND,
    CI_QUALITY_CONFIG_COMMAND,
)


ROOT = Path(__file__).parents[2]


def test_orchestrator_and_test_modules_respect_size_limits() -> None:
    assert file_size_violations(ROOT) == []


def test_platform_import_boundaries_are_acyclic() -> None:
    assert import_boundary_violations(ROOT) == []


def test_parsl_import_boundaries_cover_core_and_shared_platform_layers(
    tmp_path: Path,
) -> None:
    sources = {
        "packages/bioimageflow/bioimageflow/storage/leak.py": (
            "from bioimageflow.parsl import ParslEngine\n"
        ),
        "packages/bioimageflow/bioimageflow/cache/leak.py": (
            "def load():\n    import parsl\n"
        ),
        "packages/bioimageflow/bioimageflow/engine/leak.py": "import parsl\n",
        "packages/bioimageflow-core/bioimageflow_core/leak.py": (
            "def load():\n    from parsl import python_app\n"
        ),
    }
    for relative_path, source in sources.items():
        path = tmp_path / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source)

    failures = import_boundary_violations(tmp_path)

    assert len(failures) == 4
    assert len({failure.split(": forbidden import", 1)[0] for failure in failures}) == 4
    assert all("forbidden import" in failure for failure in failures)


def test_external_parsl_import_is_lazy_outside_the_parsl_package(
    tmp_path: Path,
) -> None:
    top_level = (
        tmp_path
        / "packages"
        / "bioimageflow-example"
        / "bioimageflow_example"
        / "runtime.py"
    )
    top_level.parent.mkdir(parents=True)
    top_level.write_text("import parsl\n")
    test_module = (
        tmp_path
        / "packages"
        / "bioimageflow-example"
        / "tests"
        / "test_runtime.py"
    )
    test_module.parent.mkdir(parents=True)
    test_module.write_text("import parsl\n")

    assert import_boundary_violations(tmp_path) == [
        "packages/bioimageflow-example/bioimageflow_example/runtime.py:1: "
        "forbidden import parsl"
    ]


def test_every_orchestrator_module_has_affected_test_ownership() -> None:
    assert unowned_platform_paths(root=ROOT) == []


def test_relative_imports_and_guarded_worker_imports_respect_actual_boundaries(
    tmp_path: Path,
) -> None:
    sources = {
        "packages/bioimageflow/bioimageflow/storage/relative.py": (
            "from .. import engine\nfrom ..engine import scheduler\n"
        ),
        "packages/bioimageflow-core/bioimageflow_core/worker.py": (
            "try:\n    import pandas\nexcept ImportError:\n    pass\n"
            "def invoke():\n    from bioimageflow import Workflow\n"
        ),
        "packages/bioimageflow-core/bioimageflow_core/declaration.py": (
            "if True:\n    import pydantic\n"
            "def scientific_callback():\n    import pandas\n"
        ),
    }
    for relative, source in sources.items():
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source)

    failures = import_boundary_violations(tmp_path)

    assert len({item.split(": forbidden import", 1)[0] for item in failures}) == 5
    assert any("relative.py:1: forbidden import bioimageflow.engine" in item for item in failures)
    assert any("relative.py:2: forbidden import bioimageflow.engine" in item for item in failures)
    assert any("worker.py:2: forbidden import pandas" in item for item in failures)
    assert any("worker.py:6: forbidden import bioimageflow" in item for item in failures)
    assert any("declaration.py:2: forbidden import pydantic" in item for item in failures)


def test_every_orchestrator_module_has_exactly_one_source_owner() -> None:
    assert nonuniquely_owned_platform_paths(root=ROOT) == []


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


def test_declared_source_owners_precede_docs_and_example_fallbacks():
    cases = {
        "docs/source/workflows/images/generate_workflow_previews.py": "tests/unit/test_workflow_preview_binding.py",
        "example_workflows/fish_analysis/tools/download_images.py": "tests/unit/test_example_download_tools.py",
    }
    for path, test in cases.items():
        commands = commands_for_paths([path], stage="edit", root=ROOT)
        assert len(commands) == 1 and test in commands[0]
        assert FAST_TEST_COMMAND not in commands and DOCS_BUILD_COMMAND not in commands


def test_existing_collected_test_edits_select_only_the_actual_file():
    for path in [
        "tests/unit/test_development_workflow.py",
        "packages/bioimageflow-spot-tools/tests/test_atlas_workdir.py",
    ]:
        assert commands_for_paths([path], stage="edit", root=ROOT) == [
            f"uv run pytest {path}"
        ]


def test_shared_support_and_configuration_keep_broad_guards():
    broad = [FAST_TEST_COMMAND, PARSL_FAST_TEST_COMMAND]
    for path in ["tests/support/ci_selectors.py", "tests/testkit/primary_content.py"]:
        assert commands_for_paths([path], stage="edit", root=ROOT) == broad
    assert commands_for_paths(["conftest.py"], stage="edit", root=ROOT) == [
        CI_QUALITY_CONFIG_COMMAND,
        DOCS_BUILD_COMMAND,
    ]
    package = "packages/bioimageflow-segmentation-tools/tests/conftest.py"
    commands = commands_for_paths([package], stage="edit", root=ROOT)
    assert len(commands) == 2
    assert (
        'packages/bioimageflow-segmentation-tools/tests -m "not complete"'
        in commands[0]
    )
    assert commands[0] != f"uv run pytest {package}"


def test_prose_readme_and_docs_edits_select_only_strict_docs():
    for path in [
        "README.md",
        "packages/bioimageflow-io-tools/README.md",
        "example_workflows/parameter_space_exploration/README.md",
        "docs/source/specs.md",
        "docs/source/reference/api/core.rst",
    ]:
        assert commands_for_paths([path], stage="edit", root=ROOT) == [
            DOCS_BUILD_COMMAND
        ]


def test_precommit_stage_selects_independent_suites() -> None:
    commands = commands_for_paths(
        ["packages/bioimageflow/bioimageflow/storage/manifests.py"],
        stage="precommit",
        root=ROOT,
    )
    assert commands == [UNIT_TEST_COMMAND, DIRECT_INTEGRATION_TEST_COMMAND]


def test_merge_and_unknown_paths_fail_open_to_fast_suite() -> None:
    expected = [FAST_TEST_COMMAND, PARSL_FAST_TEST_COMMAND]
    assert commands_for_paths(["unknown.file"], root=ROOT) == expected
    assert commands_for_paths(
        ["packages/bioimageflow/bioimageflow/cache/identity.py"],
        stage="merge",
        root=ROOT,
    ) == expected


def test_parsl_precommit_stage_selects_fake_and_real_runtime_tiers() -> None:
    assert commands_for_paths(
        ["packages/bioimageflow/bioimageflow/parsl/engine.py"],
        stage="precommit",
        root=ROOT,
    ) == [UNIT_TEST_COMMAND, PARSL_FAST_TEST_COMMAND]
