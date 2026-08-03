from __future__ import annotations

import base64
from pathlib import Path, PurePosixPath

import pytest

import bioimageflow.launcher.ssh as ssh_module
from bioimageflow import (
    NodeFailureDiagnostic,
    RemoteWorkflowRun,
    SSHSubmissionTransport,
)


RUN_ID = "run_1234567812344abc923456789abcdef0"
STORAGE = "/cluster/project/results"


def _transport() -> SSHSubmissionTransport:
    return SSHSubmissionTransport(
        host="hpc",
        staging_root=PurePosixPath("/cluster/staging"),
        remote_executable=PurePosixPath("/cluster/bin/bioimageflow-cluster-agent"),
    )


def _observation(state: str = "running") -> dict:
    return {
        "error": None,
        "run_id": RUN_ID,
        "state": state,
        "status_revision": 2,
        "storage_path": STORAGE,
        "submission_schema": "bioimageflow.launcher.submission.v2",
        "status_schema": "bioimageflow.launcher.status.v1",
        "terminal": state in {"succeeded", "failed", "cancelled", "lost"},
        "updated_at": "2026-07-29T12:00:00Z",
    }


def test_open_refresh_and_reconnect_have_no_local_path_claims(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    states = iter(("running", "succeeded"))

    def execute(transport, operation, arguments, *, request_id):
        del transport, request_id
        assert arguments == {"run_id": RUN_ID, "storage_path": STORAGE}
        return _observation(next(states))

    monkeypatch.setattr(ssh_module, "execute_cluster_command", execute)
    run = RemoteWorkflowRun.open(_transport(), STORAGE, RUN_ID)

    assert run.status == "running"
    assert not hasattr(run, "control_dir")
    assert not hasattr(run, "view_dir")
    run.refresh()
    assert run.status == "succeeded"


def test_wait_uses_repeated_authoritative_refresh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    states = iter(("running", "running", "succeeded"))

    def execute(*args, **kwargs):
        return _observation(next(states))

    monkeypatch.setattr(ssh_module, "execute_cluster_command", execute)
    run = RemoteWorkflowRun.open(_transport(), STORAGE, RUN_ID)

    assert run.wait(timeout=1, poll_interval=0.001) == "succeeded"

    with pytest.raises(ValueError):
        run.wait(poll_interval=0)


def test_progress_preserves_server_sequences_across_pages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[int] = []

    def execute(transport, operation, arguments, *, request_id):
        del transport, request_id
        if operation == "inspect":
            return _observation()
        assert operation == "read-progress"
        calls.append(arguments["after_sequence"])
        sequence = 8 if len(calls) == 1 else 11
        return {
            **_observation(),
            "events": [{"sequence": sequence, "status": "running"}],
            "has_more": len(calls) == 1,
            "next_sequence": sequence,
        }

    monkeypatch.setattr(ssh_module, "execute_cluster_command", execute)
    run = RemoteWorkflowRun.open(_transport(), STORAGE, RUN_ID)

    assert [event["sequence"] for event in run.progress(after_sequence=5)] == [8, 11]
    assert calls == [5, 8]


def test_reconnected_run_exposes_same_structured_diagnostics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = NodeFailureDiagnostic(
        scoped_node_path="nested/tool",
        category="worker",
        exception_type="RuntimeError",
        message="remote failure",
        traceback="remote traceback",
        attempt_id="task-7",
    )

    def execute(transport, operation, arguments, *, request_id):
        del transport, arguments, request_id
        if operation == "inspect":
            return _observation("failed")
        assert operation == "read-progress"
        return {
            **_observation("failed"),
            "events": [
                {
                    "sequence": 1,
                    "kind": "diagnostic",
                    "payload": expected.to_dict(),
                }
            ],
            "has_more": False,
            "next_sequence": 1,
        }

    monkeypatch.setattr(ssh_module, "execute_cluster_command", execute)
    run = RemoteWorkflowRun.open(_transport(), STORAGE, RUN_ID)

    assert run.diagnostics() == (expected,)


def test_logs_assemble_bytes_before_replacement_decoding_and_restart_rotation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stdout_calls = 0

    def execute(transport, operation, arguments, *, request_id):
        nonlocal stdout_calls
        del transport, request_id
        if operation == "inspect":
            return _observation()
        stream = arguments["stream"]
        if stream == "stderr":
            return {
                **_observation(),
                "data": "",
                "eof": True,
                "exists": False,
                "identity": None,
                "next_offset": 0,
                "reset": False,
                "snapshot_size": 0,
                "stream": stream,
            }
        stdout_calls += 1
        chunks = (b"\xe2", b"\x82\xac")
        chunk = chunks[stdout_calls - 1]
        return {
            **_observation(),
            "data": base64.b64encode(chunk).decode(),
            "eof": stdout_calls == 2,
            "exists": True,
            "identity": "1:2",
            "next_offset": arguments["offset"] + len(chunk),
            "reset": False,
            "snapshot_size": 3,
            "stream": stream,
        }

    monkeypatch.setattr(ssh_module, "execute_cluster_command", execute)
    run = RemoteWorkflowRun.open(_transport(), STORAGE, RUN_ID)

    assert run.logs() == "[stdout]\n€"


def test_cancel_reuses_mutation_retry_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = {}

    def execute(*args, **kwargs):
        return _observation()

    def retry(transport, operation, arguments, request_id):
        del transport
        captured.update(
            operation=operation,
            arguments=arguments,
            request_id=request_id,
        )
        return _observation("cancelled")

    monkeypatch.setattr(ssh_module, "execute_cluster_command", execute)
    monkeypatch.setattr(ssh_module, "_retry_mutation", retry)
    run = RemoteWorkflowRun.open(_transport(), STORAGE, RUN_ID)
    run.cancel()

    assert run.status == "cancelled"
    assert captured["operation"] == "cancel"
    assert captured["arguments"]["staging_root"] == "/cluster/staging"


@pytest.mark.parametrize("path", ["relative", "//cluster/path", "/cluster/a/../path"])
def test_open_rejects_noncanonical_cluster_storage(path: str) -> None:
    with pytest.raises((TypeError, ValueError)):
        RemoteWorkflowRun.open(_transport(), path, RUN_ID)


def test_result_requires_absent_destination_parent_to_be_created_by_caller(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def execute(*args, **kwargs):
        return _observation("running")

    monkeypatch.setattr(ssh_module, "execute_cluster_command", execute)
    run = RemoteWorkflowRun.open(_transport(), STORAGE, RUN_ID)

    with pytest.raises(Exception, match="not succeeded"):
        run.result(destination=tmp_path / "result")
