"""Tests for the optional complete-test pytest tier."""

from pathlib import Path


pytest_plugins = ["pytester"]


def _root_conftest() -> str:
    return (Path(__file__).resolve().parents[2] / "conftest.py").read_text()


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
