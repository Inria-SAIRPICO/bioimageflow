"""Tests for the optional complete-test pytest tier."""

from pathlib import Path

import pytest


pytest_plugins = ["pytester"]


EXTERNAL_TIER_MARKERS = (
    "wetlands",
    "complete",
    "public_data",
    "external_binary",
    "sairpico_binary",
    "model_runtime",
)
NON_COMPLETE_GATED_MARKERS = (
    "acceptance",
    "package_tools",
    "parsl",
    "packaging",
    "shared_memory",
    "slow",
)

COMPLETE_RESOURCE_TEST_FILES = (
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


def test_external_tier_marker_list_matches_root_conftest() -> None:
    namespace: dict[str, object] = {}
    exec(_root_conftest(), namespace)

    assert namespace["EXTERNAL_TEST_MARKER_NAMES"] == set(EXTERNAL_TIER_MARKERS)


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
    result.stdout.fnmatch_lines(["*external tests require --run-complete*"])


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


@pytest.mark.parametrize("marker", EXTERNAL_TIER_MARKERS)
def test_external_tier_markers_skip_by_default(pytester, marker: str) -> None:
    pytester.makeconftest(_root_conftest())
    pytester.makepyfile(
        f"""
        import pytest

        def test_regular():
            assert True

        @pytest.mark.{marker}
        def test_external_tier():
            assert False
        """
    )

    result = pytester.runpytest("-rs")

    result.assert_outcomes(passed=1, skipped=1)
    result.stdout.fnmatch_lines(["*external tests require --run-complete*"])


@pytest.mark.parametrize("marker", EXTERNAL_TIER_MARKERS)
def test_run_complete_enables_external_tier_markers(
    pytester,
    marker: str,
) -> None:
    pytester.makeconftest(_root_conftest())
    pytester.makepyfile(
        f"""
        import pytest

        @pytest.mark.{marker}
        def test_external_tier():
            assert False
        """
    )

    result = pytester.runpytest("--run-complete")

    result.assert_outcomes(failed=1)


@pytest.mark.parametrize("marker", NON_COMPLETE_GATED_MARKERS)
def test_deterministic_runtime_markers_are_not_complete_gated(
    pytester,
    marker: str,
) -> None:
    pytester.makeconftest(_root_conftest())
    pytester.makepyfile(
        f"""
        import pytest

        @pytest.mark.{marker}
        def test_deterministic_non_fast():
            assert True
        """
    )

    result = pytester.runpytest("-rs")

    result.assert_outcomes(passed=1)


def test_external_marker_names_in_param_ids_do_not_skip_regular_tests(
    pytester,
) -> None:
    pytester.makeconftest(_root_conftest())
    pytester.makepyfile(
        """
        import pytest

        @pytest.mark.parametrize("name", ["wetlands", "model_runtime"])
        def test_regular_parametrized_name(name):
            assert name
        """
    )

    result = pytester.runpytest()

    result.assert_outcomes(passed=2)


def test_param_level_external_marker_skips_by_default(pytester) -> None:
    pytester.makeconftest(_root_conftest())
    pytester.makepyfile(
        """
        import pytest

        @pytest.mark.parametrize(
            "name",
            [
                "regular",
                pytest.param("external", marks=pytest.mark.public_data),
            ],
        )
        def test_param_level_marker(name):
            assert name == "regular"
        """
    )

    result = pytester.runpytest("-rs")

    result.assert_outcomes(passed=1, skipped=1)
    result.stdout.fnmatch_lines(["*external tests require --run-complete*"])


def test_complete_resource_test_manifest_paths_exist() -> None:
    root = _repo_root()

    missing = [
        relative_path
        for relative_path in COMPLETE_RESOURCE_TEST_FILES
        if not (root / relative_path).is_file()
    ]

    assert missing == []


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
