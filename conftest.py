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


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--run-complete",
        action="store_true",
        default=False,
        help="run complete tests marked with @pytest.mark.complete",
    )


def pytest_configure(config: pytest.Config) -> None:
    for marker in COMPLETE_TEST_MARKERS:
        config.addinivalue_line("markers", marker)


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    if config.getoption("--run-complete"):
        return

    skip_complete = pytest.mark.skip(
        reason="complete tests require --run-complete"
    )
    for item in items:
        if "complete" in item.keywords:
            item.add_marker(skip_complete)


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
