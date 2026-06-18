"""Repository-wide pytest configuration."""

from __future__ import annotations

from pathlib import Path
from collections.abc import Iterator
from typing import Any

import pytest


COMPLETE_TEST_MARKERS = [
    "wetlands: tests that execute real Wetlands worker processes",
    "complete: optional complete tests that may use real binaries, public data, model runtimes, or longer workflows",
    "public_data: tests that download or use public datasets",
    "external_binary: tests that require non-Python command-line tools",
    "sairpico_binary: tests that require real SAIRPICO command-line tools",
    "model_runtime: tests that require optional model runtimes or model downloads",
]
REGISTERED_TEST_MARKERS = [
    *COMPLETE_TEST_MARKERS,
    "slow: deterministic or external tests excluded from the fast development loop",
    "acceptance: deterministic high-level workflow or example coverage excluded from the fast development loop",
    "packaging: build artifact, wheel, sdist, or package metadata artifact checks",
    "package_tools: tests owned by optional tool packages",
    "shared_memory: deterministic tests requiring POSIX/shared-memory platform support",
]

EXTERNAL_TEST_MARKER_NAMES = frozenset(
    marker.split(":", 1)[0] for marker in COMPLETE_TEST_MARKERS
)


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--run-complete",
        action="store_true",
        default=False,
        help="run complete and external tests",
    )


def pytest_configure(config: pytest.Config) -> None:
    for marker in REGISTERED_TEST_MARKERS:
        config.addinivalue_line("markers", marker)


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    if config.getoption("--run-complete"):
        return

    skip_external = pytest.mark.skip(
        reason="external tests require --run-complete"
    )
    for item in items:
        marker_names = {marker.name for marker in item.iter_markers()}
        if EXTERNAL_TEST_MARKER_NAMES.intersection(marker_names):
            item.add_marker(skip_external)


@pytest.fixture
def complete_wetlands_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[dict[str, Any]]:
    """Return an isolated Wetlands config for complete portability tests."""
    from bioimageflow.env_manager import _reset_shared_manager

    home = tmp_path / "bioimageflow-home"
    wetlands_root = home / "wetlands"
    tool_store = home / "tool_packages"

    monkeypatch.setenv("BIOIMAGEFLOW_HOME", str(home))
    monkeypatch.setenv("BIOIMAGEFLOW_WETLANDS", str(wetlands_root))
    monkeypatch.setenv("BIOIMAGEFLOW_TOOL_STORE", str(tool_store))

    _reset_shared_manager()
    try:
        yield {
            "wetlands_instance_path": wetlands_root,
            "use_local_bioimageflow_core": True,
        }
    finally:
        _reset_shared_manager()
