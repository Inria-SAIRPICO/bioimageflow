from __future__ import annotations

import base64
from pathlib import Path, PurePosixPath

import pytest

import bioimageflow.launcher.ssh as ssh_module
from bioimageflow import (
    NodeFailureDiagnostic,
    PSIJSubmissionUncertainError,
    RemoteWorkflowRun,
    RecomputeRequest,
    RunRetryPlan,
    SSHSubmissionTransport,
    SSHTransportError,
    WorkflowResultExportError,
    WorkflowResultIntegrityError,
    WorkflowRunRetryError,
)


RUN_ID = "run_1234567812344abc923456789abcdef0"
STORAGE = "/cluster/project/results"


def _transport() -> SSHSubmissionTransport:
    return SSHSubmissionTransport(
        host="hpc",
        staging_root=PurePosixPath("/cluster/staging"),
        remote_executable=PurePosixPath("/cluster/bin/bioimageflow-cluster-agent"),
    )


def _observation(state: str = "running", run_id: str = RUN_ID) -> dict:
    return {
        "schema": "bioimageflow.launcher.run-observation.v1",
        "error": None,
        "retry_plan": None,
        "run_id": run_id,
        "state": state,
        "status_revision": 2,
        "storage_path": STORAGE,
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


def test_remote_retry_uses_public_preview_and_idempotent_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    retry_id = "run_1234567812344abc923456789abcdeff"
    plan = RunRetryPlan(
        parent_run_id=RUN_ID,
        retry_run_id=retry_id,
        parent_status="failed",
        parent_status_revision=4,
        storage_path=STORAGE,
        retained_submission_digest="sha256:" + "1" * 64,
        retained_material_digest="sha256:" + "2" * 64,
        retained_material_entries=3,
        cache_selection_revision="sha256:" + "0" * 64,
        recompute=RecomputeRequest(("nested/tool",), cascade=True),
        invalidations=(),
        conflicting_run_ids=(),
    )
    calls: list[tuple[str, dict]] = []

    def execute(transport, operation, arguments, *, request_id):
        del transport, request_id
        calls.append((operation, arguments))
        if operation == "inspect":
            return _observation("failed")
        assert operation == "plan-retry"
        return plan.to_dict()

    def retry(transport, operation, arguments, request_id):
        del transport, request_id
        calls.append((operation, arguments))
        return {
            **_observation("prepared", retry_id),
            "retry_plan": plan.to_dict(),
        }

    monkeypatch.setattr(ssh_module, "execute_cluster_command", execute)
    monkeypatch.setattr(ssh_module, "_retry_mutation", retry)
    run = RemoteWorkflowRun.open(_transport(), STORAGE, RUN_ID)

    confirmed = run.plan_retry(plan.recompute)
    retried = run.start_retry(confirmed)

    assert retried.id == retry_id
    assert retried.parent_id == RUN_ID
    reopened = RemoteWorkflowRun(
        _transport(),
        STORAGE,
        retry_id,
        observation={
            **_observation("prepared", retry_id),
            "retry_plan": plan.to_dict(),
        },
    )
    assert reopened.parent_id == RUN_ID
    assert reopened.retry_plan == plan
    assert calls[-1][0] == "start-retry"
    assert calls[-1][1]["plan"] == plan.to_dict()


def test_remote_retry_translates_public_conflict_and_uncertainty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    retry_id = "run_1234567812344abc923456789abcdeff"
    plan = RunRetryPlan(
        parent_run_id=RUN_ID,
        retry_run_id=retry_id,
        parent_status="failed",
        parent_status_revision=4,
        storage_path=STORAGE,
        retained_submission_digest="sha256:" + "1" * 64,
        retained_material_digest="sha256:" + "2" * 64,
        retained_material_entries=3,
        cache_selection_revision="sha256:" + "0" * 64,
        recompute=None,
        invalidations=(),
        conflicting_run_ids=(),
    )
    calls = 0

    def execute(*args, **kwargs):
        nonlocal calls
        del args, kwargs
        calls += 1
        if calls == 1:
            return _observation("failed")
        raise SSHTransportError("remote-retry-conflict", "active run")

    monkeypatch.setattr(ssh_module, "execute_cluster_command", execute)
    run = RemoteWorkflowRun.open(_transport(), STORAGE, RUN_ID)
    with pytest.raises(WorkflowRunRetryError, match="active run"):
        run.plan_retry()

    monkeypatch.setattr(
        ssh_module,
        "_retry_mutation",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            SSHTransportError(
                "remote-retry-submission-uncertain",
                "scheduler outcome unknown",
            )
        ),
    )
    with pytest.raises(PSIJSubmissionUncertainError, match="scheduler outcome"):
        run.start_retry(plan)


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
        run.export_result(tmp_path / "result")


@pytest.mark.parametrize(
    ("remote_code", "expected_error"),
    [
        ("remote-result-integrity", WorkflowResultIntegrityError),
        ("remote-result-mutated", WorkflowResultIntegrityError),
        ("remote-result-too-large", WorkflowResultExportError),
    ],
)
def test_result_preparation_translates_remote_domain_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    remote_code: str,
    expected_error: type[Exception],
) -> None:
    monkeypatch.setattr(
        ssh_module,
        "execute_cluster_command",
        lambda *args, **kwargs: _observation("succeeded"),
    )
    monkeypatch.setattr(
        ssh_module,
        "_retry_mutation",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            SSHTransportError(remote_code, "sanitized remote failure")
        ),
    )
    run = RemoteWorkflowRun.open(_transport(), STORAGE, RUN_ID)

    with pytest.raises(expected_error) as caught:
        run.export_result(tmp_path / "result")

    assert caught.value.details["remote_code"] == remote_code
