"""Tests for the optional complete-test pytest tier."""

from pathlib import Path


pytest_plugins = ["pytester"]


COMPLETE_RESOURCE_TEST_FILES = (
    "tests/priority_workflows/test_complete_workflows.py",
    "packages/bioimageflow-common-tools/tests/test_common_complete_wetlands.py",
    "packages/bioimageflow-segmentation-tools/tests/test_execution.py",
    "packages/bioimageflow-sairpico-tools/tests/test_sairpico_complete_binary_tools.py",
)

FORBIDDEN_COMPLETE_RESOURCE_PATTERNS = (
    "_require_modules",
    "_require_commands",
    "_require_sairpico_binaries",
    "shutil.which",
    "importlib.util.find_spec",
    "wf.use_wetlands = False",
)


def _root_conftest() -> str:
    return (Path(__file__).resolve().parents[2] / "conftest.py").read_text()


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def test_complete_tests_skip_by_default(pytester) -> None:
    pytester.makeconftest(_root_conftest())
    pytester.makepyfile(
        """
        import pytest

        def test_regular():
            assert True

        @pytest.mark.complete
        def test_complete():
            assert False
        """
    )

    result = pytester.runpytest("-rs")

    result.assert_outcomes(passed=1, skipped=1)
    result.stdout.fnmatch_lines(["*complete tests require --run-complete*"])


def test_complete_tests_run_when_enabled(pytester) -> None:
    pytester.makeconftest(_root_conftest())
    pytester.makepyfile(
        """
        import pytest

        def test_regular():
            assert True

        @pytest.mark.complete
        def test_complete():
            assert False
        """
    )

    result = pytester.runpytest("--run-complete")

    result.assert_outcomes(passed=1, failed=1)


def test_complete_resource_tests_do_not_gate_on_host_runtimes() -> None:
    root = _repo_root()

    violations = []
    for relative_path in COMPLETE_RESOURCE_TEST_FILES:
        text = (root / relative_path).read_text()
        for pattern in FORBIDDEN_COMPLETE_RESOURCE_PATTERNS:
            if pattern in text:
                violations.append(f"{relative_path}: {pattern}")

    assert violations == []


def test_complete_resource_tests_are_wetlands_marked() -> None:
    root = _repo_root()

    missing = [
        relative_path
        for relative_path in COMPLETE_RESOURCE_TEST_FILES
        if "pytest.mark.wetlands" not in (root / relative_path).read_text()
    ]

    assert missing == []
