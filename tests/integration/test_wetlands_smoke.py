"""Small published-Wetlands smoke test.

This is the lightweight certification tier for the real ProcessingTool ->
Wetlands worker path. The broader task API suite remains in
test_wetlands_task_api.py.
"""

from __future__ import annotations

import os
import logging
from collections.abc import Iterator
from importlib.metadata import version
from pathlib import Path

import pytest
from packaging.version import Version

from bioimageflow import Workflow, configure_logging
from bioimageflow.env_manager import _reset_shared_manager

from tests.testkit.integration_tools import FileLoader
from .wetlands_test_tools import SimpleRowTool, WorkerStreamTool

pytestmark = pytest.mark.wetlands


def _snapshot_loggers() -> dict[str, tuple[list[logging.Handler], int, bool]]:
    return {
        name: (logger.handlers[:], logger.level, logger.propagate)
        for name in ("bioimageflow", "wetlands")
        for logger in [logging.getLogger(name)]
    }


def _restore_loggers(
    snapshot: dict[str, tuple[list[logging.Handler], int, bool]],
) -> None:
    for name, (handlers, level, propagate) in snapshot.items():
        logger = logging.getLogger(name)
        for handler in logger.handlers[:]:
            logger.removeHandler(handler)
            if handler not in handlers:
                handler.close()
        logger.handlers[:] = handlers
        logger.setLevel(level)
        logger.propagate = propagate


@pytest.fixture(autouse=True)
def _disable_wetlands() -> Iterator[None]:
    """Override the integration conftest patch that disables Wetlands."""
    yield


@pytest.fixture(autouse=True)
def _isolated_wetlands_roots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[None]:
    home = Path(os.environ.get("BIOIMAGEFLOW_HOME", tmp_path / "bioimageflow-home"))
    wetlands_root = Path(os.environ.get("BIOIMAGEFLOW_WETLANDS", home / "wetlands"))
    tool_store = Path(os.environ.get("BIOIMAGEFLOW_TOOL_STORE", home / "tool_packages"))

    monkeypatch.setenv("BIOIMAGEFLOW_HOME", str(home))
    monkeypatch.setenv("BIOIMAGEFLOW_WETLANDS", str(wetlands_root))
    monkeypatch.setenv("BIOIMAGEFLOW_TOOL_STORE", str(tool_store))

    _reset_shared_manager()
    yield
    _reset_shared_manager()


def test_processing_tool_executes_through_published_wetlands(tmp_path: Path) -> None:
    assert Version(version("wetlands")) >= Version("1.0.1")

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    for name in ["img_01.tif", "img_02.tif", "img_03.tif"]:
        (data_dir / name).write_text(f"FAKE_{name}")

    with Workflow(
        storage_path=tmp_path / "results", engine="wetlands",
        max_workers=1,
    ) as wf:
        raw = FileLoader()(path=str(data_dir))
        out = SimpleRowTool()(input_path=raw["path"])
        df = wf.compute(out)

    assert len(df) == 3
    assert set(df["value"]) == {42.0}
    for output_path in df["output_path"]:
        path = Path(output_path)
        assert path.exists()
        assert path.read_text().startswith("processed:")


def test_configured_logging_routes_wetlands_worker_streams(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert Version(version("wetlands")) >= Version("1.1.0")

    snapshot = _snapshot_loggers()
    try:
        configure_logging(fmt="%(levelname)s:%(message)s", force=True)
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        (data_dir / "img_01.tif").write_text("FAKE")

        with Workflow(
            storage_path=tmp_path / "results", engine="wetlands",
            max_workers=1,
        ) as wf:
            raw = FileLoader()(path=str(data_dir))
            out = WorkerStreamTool()(input_path=raw["path"])
            df = wf.compute(out)

        assert len(df) == 1
        captured = capsys.readouterr()
        assert "INFO:worker routine stdout" in captured.out
        assert "INFO:worker actual stderr" in captured.out
        assert captured.err == ""
    finally:
        _restore_loggers(snapshot)
