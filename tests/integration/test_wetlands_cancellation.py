"""Real-Wetlands cancellation propagation and drain regressions."""

import json
import socket
import time
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import BinaryIO

import pytest

from bioimageflow import ProgressEvent, Workflow
from bioimageflow.engine import WorkflowCancelledError
from bioimageflow.env_manager import _reset_shared_manager
from tests.testkit.integration_tools import FileLoader

from .wetlands_test_tools import CancellableBatchTool, CancellableRowTool

pytestmark = [pytest.mark.complete, pytest.mark.wetlands]


@pytest.fixture(autouse=True)
def _disable_wetlands(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Override the integration fixture that disables Wetlands."""
    core_source = Path(__file__).resolve().parents[2] / "packages" / "bioimageflow-core"
    monkeypatch.setenv("BIOIMAGEFLOW_CORE_SOURCE", str(core_source))
    monkeypatch.setenv("BIOIMAGEFLOW_WETLANDS", str(tmp_path / "wetlands"))
    _reset_shared_manager()
    try:
        yield
    finally:
        _reset_shared_manager()


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    for name in ["img_01.tif", "img_02.tif", "img_03.tif"]:
        (data_dir / name).write_text(f"FAKE_{name}")
    return tmp_path


def test_cancel_reaches_active_rows_and_drains_submitted_window(
    workspace: Path,
) -> None:
    """Every submitted mapped task is cancelled while row waits are blocked."""
    events: list[ProgressEvent] = []
    load = FileLoader()
    tool = CancellableRowTool()

    with socket.create_server(("127.0.0.1", 0)) as listener:
        listener.settimeout(60)
        control_port = listener.getsockname()[1]
        with Workflow(
            storage_path=workspace / "results",
            engine="wetlands",
            max_workers=2,
            on_progress=events.append,
        ) as workflow:
            raw = load(path=str(workspace / "data"))
            output = tool(input_path=raw["path"], control_port=control_port)
            controls: list[tuple[socket.socket, BinaryIO]] = []
            with ThreadPoolExecutor(max_workers=1) as executor:
                result = executor.submit(workflow.compute, output)
                try:
                    for _ in range(2):
                        control, reader = _accept_worker(listener, result)
                        controls.append((control, reader))
                        assert _read_control_event(reader)["event"] == "started"

                    workflow.cancel()
                    for control, reader in controls:
                        _release_cancelled_worker(control, reader, result)

                    with pytest.raises(WorkflowCancelledError):
                        result.result(timeout=10)
                finally:
                    for control, reader in controls:
                        reader.close()
                        control.close()

    assert any(event.status == "cancelled" for event in events)
    assert not list((workspace / "results").rglob("*_cancel_*.txt"))


def test_cancel_reaches_active_batch_and_drains_it(workspace: Path) -> None:
    """A blocked process_batch task receives cooperative cancellation."""
    load = FileLoader()
    tool = CancellableBatchTool()

    with socket.create_server(("127.0.0.1", 0)) as listener:
        listener.settimeout(60)
        control_port = listener.getsockname()[1]
        with Workflow(
            storage_path=workspace / "batch_results",
            engine="wetlands",
        ) as workflow:
            raw = load(path=str(workspace / "data"))
            output = tool(input_path=raw["path"], control_port=control_port)
            with ThreadPoolExecutor(max_workers=1) as executor:
                result = executor.submit(workflow.compute, output)
                control, reader = _accept_worker(listener, result)
                try:
                    assert _read_control_event(reader) == {
                        "event": "started",
                        "label": "batch:3",
                    }
                    workflow.cancel()
                    _release_cancelled_worker(control, reader, result)
                    with pytest.raises(WorkflowCancelledError):
                        result.result(timeout=10)
                finally:
                    reader.close()
                    control.close()

    assert not list((workspace / "batch_results").rglob("*_cancel_batch_*.txt"))


def _accept_worker(
    listener: socket.socket,
    result: Future[object],
) -> tuple[socket.socket, BinaryIO]:
    try:
        control, _ = listener.accept()
    except TimeoutError:
        if result.done():
            result.result()
        raise
    control.settimeout(10)
    return control, control.makefile("rb")


def _read_control_event(reader: BinaryIO) -> dict[str, object]:
    data = reader.readline()
    if not data:
        raise AssertionError("Wetlands worker closed its control socket")
    return json.loads(data)


def _release_cancelled_worker(
    control: socket.socket,
    reader: BinaryIO,
    result: Future[object],
) -> None:
    deadline = time.monotonic() + 10
    while True:
        control.sendall(b"probe\n")
        event = _read_control_event(reader)
        if event["event"] == "pending":
            if time.monotonic() >= deadline:
                control.sendall(b"abort\n")
                raise AssertionError("Wetlands worker did not observe cancellation")
            continue
        assert event == {"event": "cancellation_observed"}
        assert not result.done(), "compute returned before its active writer drained"
        control.sendall(b"finish\n")
        assert _read_control_event(reader) == {"event": "cancellation_acknowledged"}
        return
