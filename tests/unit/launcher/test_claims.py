from __future__ import annotations

import multiprocessing
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import bioimageflow.launcher.control as control_module
from bioimageflow.launcher.errors import LauncherStateConflictError
from bioimageflow.launcher.repository import (
    ClaimConflictError,
    ClaimExpiredError,
    LauncherRepository,
)
from bioimageflow.launcher.schemas import SUBMISSION_SCHEMA, utc_timestamp
from bioimageflow.launcher.state import (
    ClaimEpochMismatchError,
    InvalidTransitionError,
)


def _submission(storage_root: Path, run_id: str) -> dict[str, object]:
    return {
        "schema": SUBMISSION_SCHEMA,
        "run_id": run_id,
        "created_at": "2026-07-28T07:00:00Z",
        "storage_root": str(storage_root.resolve()),
        "canonical_view": f"views/runs/{run_id}",
        "workflow": {
            "kind": "graph",
            "digest": "sha256:" + "c" * 64,
            "payload": {"schema_version": 1},
        },
        "invocation": {"kind": "root", "inputs": {}},
        "parsl_config": {"factory": "tests:config"},
        "executor_bindings": {},
        "node_routes": None,
        "environment_routes": None,
        "shared_runtime_root": None,
        "task_policy": {},
        "launch": {"backend": "local"},
        "protocol_versions": {"launcher": 1},
    }


def _control(tmp_path: Path):
    repository = LauncherRepository(tmp_path)
    run_id = repository.new_run_id()
    return repository.allocate(
        _submission(repository.storage_root, run_id),
        backend="local",
    )


def _claim_worker(
    storage_root: str,
    run_id: str,
    owner: str,
    ready: multiprocessing.Queue,
    start: multiprocessing.Event,
    results: multiprocessing.Queue,
) -> None:
    control = LauncherRepository(storage_root).open(run_id)
    ready.put(owner)
    start.wait(timeout=10)
    try:
        control.claim_start(
            expected_revision=0,
            owner=owner,
            backend=f"local:{owner}",
            lease_seconds=30,
        )
    except LauncherStateConflictError:
        results.put("rejected")
    else:
        results.put("claimed")


def test_claim_start_commits_lease_and_starting_together(tmp_path: Path) -> None:
    control = _control(tmp_path)
    started = datetime(2026, 7, 28, 8, tzinfo=timezone.utc)

    result = control.claim_start(
        expected_revision=0,
        owner="host-a:123:nonce",
        backend="local:123",
        lease_seconds=20,
        now=started,
    )

    assert result.claim["epoch"] == 1
    assert result.claim["heartbeat_at"] == "2026-07-28T08:00:00Z"
    assert result.claim["expires_at"] == "2026-07-28T08:00:20Z"
    assert result.status["state"] == "starting"
    assert result.status["claim_epoch"] == 1
    assert result.status["orchestrator"] == "host-a:123:nonce"
    assert control.read_claim() == result.claim


def test_concurrent_orchestrators_have_one_claim_winner(
    tmp_path: Path,
) -> None:
    control = _control(tmp_path)
    context = multiprocessing.get_context("spawn")
    ready = context.Queue()
    start = context.Event()
    results = context.Queue()
    processes = [
        context.Process(
            target=_claim_worker,
            args=(
                str(tmp_path.resolve()),
                control.run_id,
                f"orchestrator-{index}",
                ready,
                start,
                results,
            ),
        )
        for index in range(2)
    ]

    for process in processes:
        process.start()
    for _ in processes:
        ready.get(timeout=15)
    start.set()
    outcomes = [results.get(timeout=15) for _ in processes]
    for process in processes:
        process.join(timeout=15)
        assert process.exitcode == 0

    assert sorted(outcomes) == ["claimed", "rejected"]
    assert control.read_status()["state"] == "starting"
    assert control.read_claim()["epoch"] == 1


def test_live_claim_excludes_a_second_start(tmp_path: Path) -> None:
    control = _control(tmp_path)
    now = datetime(2026, 7, 28, 8, tzinfo=timezone.utc)
    control.claim_start(
        expected_revision=0,
        owner="orchestrator-a",
        backend="local:123",
        lease_seconds=20,
        now=now,
    )

    with pytest.raises(InvalidTransitionError, match="prepared"):
        control.claim_start(
            expected_revision=1,
            owner="orchestrator-b",
            backend="local:456",
            lease_seconds=20,
            now=now,
        )


def test_heartbeat_requires_current_unexpired_epoch_and_nonce(
    tmp_path: Path,
) -> None:
    control = _control(tmp_path)
    now = datetime(2026, 7, 28, 8, tzinfo=timezone.utc)
    result = control.claim_start(
        expected_revision=0,
        owner="orchestrator-a",
        backend="local:123",
        lease_seconds=20,
        now=now,
    )

    renewed = control.heartbeat_claim(
        expected_epoch=1,
        expected_nonce=result.claim["nonce"],
        lease_seconds=30,
        now=now + timedelta(seconds=10),
    )
    assert renewed["heartbeat_at"] == "2026-07-28T08:00:10Z"
    assert renewed["expires_at"] == "2026-07-28T08:00:40Z"

    with pytest.raises(ClaimEpochMismatchError):
        control.heartbeat_claim(
            expected_epoch=2,
            expected_nonce=result.claim["nonce"],
            lease_seconds=30,
            now=now + timedelta(seconds=11),
        )
    with pytest.raises(ClaimConflictError, match="nonce"):
        control.heartbeat_claim(
            expected_epoch=1,
            expected_nonce="wrong",
            lease_seconds=30,
            now=now + timedelta(seconds=11),
        )
    with pytest.raises(ClaimExpiredError):
        control.heartbeat_claim(
            expected_epoch=1,
            expected_nonce=result.claim["nonce"],
            lease_seconds=30,
            now=now + timedelta(seconds=41),
        )


def test_recovery_takeover_archives_claim_and_advances_epoch(
    tmp_path: Path,
) -> None:
    control = _control(tmp_path)
    now = datetime(2026, 7, 28, 8, tzinfo=timezone.utc)
    first = control.claim_start(
        expected_revision=0,
        owner="orchestrator-a",
        backend="local:123",
        lease_seconds=10,
        now=now,
    )
    running = control.transition(
        expected_revision=1,
        expected_claim_epoch=1,
        new_state="running",
        updated_at=utc_timestamp(now + timedelta(seconds=1)),
    )

    with pytest.raises(ClaimConflictError, match="absence"):
        control.takeover_claim(
            expected_revision=running["revision"],
            expected_claim_epoch=1,
            owner="recovery-b",
            backend="local:recovery",
            lease_seconds=20,
            backend_absent_confirmed=False,
            now=now + timedelta(seconds=11),
        )

    recovered = control.takeover_claim(
        expected_revision=running["revision"],
        expected_claim_epoch=1,
        owner="recovery-b",
        backend="local:recovery",
        lease_seconds=20,
        backend_absent_confirmed=True,
        now=now + timedelta(seconds=11),
    )

    assert recovered.claim["epoch"] == 2
    assert recovered.status["state"] == "running"
    assert recovered.status["revision"] == running["revision"] + 1
    assert recovered.status["claim_epoch"] == 2
    assert control.read_claim_history() == [first.claim]

    with pytest.raises(ClaimEpochMismatchError):
        control.transition(
            expected_revision=recovered.status["revision"],
            expected_claim_epoch=1,
            new_state="failed",
        )


def test_unexpired_claim_cannot_be_taken_over(tmp_path: Path) -> None:
    control = _control(tmp_path)
    now = datetime(2026, 7, 28, 8, tzinfo=timezone.utc)
    control.claim_start(
        expected_revision=0,
        owner="orchestrator-a",
        backend="local:123",
        lease_seconds=30,
        now=now,
    )

    with pytest.raises(ClaimConflictError, match="still live"):
        control.takeover_claim(
            expected_revision=1,
            expected_claim_epoch=1,
            owner="recovery-b",
            backend="local:recovery",
            lease_seconds=20,
            backend_absent_confirmed=True,
            now=now + timedelta(seconds=10),
        )


def test_start_claim_resumes_after_claim_write_crash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    control = _control(tmp_path)
    now = datetime(2026, 7, 28, 8, tzinfo=timezone.utc)
    original = control_module._atomic_write_json

    def fail_status(path, payload):
        if path == control.status_path and payload.get("state") == "starting":
            raise OSError("injected status write failure")
        original(path, payload)

    monkeypatch.setattr(control_module, "_atomic_write_json", fail_status)
    with pytest.raises(OSError, match="injected"):
        control.claim_start(
            expected_revision=0,
            owner="orchestrator-a",
            backend="local:123",
            lease_seconds=20,
            now=now,
        )
    stranded = control.read_claim()
    assert stranded is not None
    assert control.read_status()["state"] == "prepared"

    monkeypatch.setattr(control_module, "_atomic_write_json", original)
    resumed = control.claim_start(
        expected_revision=0,
        owner="orchestrator-a",
        backend="local:123",
        lease_seconds=20,
        now=now + timedelta(seconds=1),
    )

    assert resumed.claim == stranded
    assert resumed.status["state"] == "starting"
    assert resumed.status["claim_epoch"] == stranded["epoch"]


def test_takeover_resumes_after_claim_write_crash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    control = _control(tmp_path)
    now = datetime(2026, 7, 28, 8, tzinfo=timezone.utc)
    first = control.claim_start(
        expected_revision=0,
        owner="orchestrator-a",
        backend="local:123",
        lease_seconds=10,
        now=now,
    )
    running = control.transition(
        expected_revision=1,
        expected_claim_epoch=1,
        new_state="running",
        updated_at=utc_timestamp(now + timedelta(seconds=1)),
    )
    original = control_module._atomic_write_json

    def fail_status(path, payload):
        if path == control.status_path and payload.get("claim_epoch") == 2:
            raise OSError("injected takeover status write failure")
        original(path, payload)

    monkeypatch.setattr(control_module, "_atomic_write_json", fail_status)
    with pytest.raises(OSError, match="injected"):
        control.takeover_claim(
            expected_revision=running["revision"],
            expected_claim_epoch=1,
            owner="recovery-b",
            backend="local:recovery",
            lease_seconds=20,
            backend_absent_confirmed=True,
            now=now + timedelta(seconds=11),
        )
    stranded = control.read_claim()
    assert stranded is not None
    assert stranded["epoch"] == 2
    assert control.read_status()["claim_epoch"] == 1
    assert control.read_claim_history() == [first.claim]

    monkeypatch.setattr(control_module, "_atomic_write_json", original)
    resumed = control.takeover_claim(
        expected_revision=running["revision"],
        expected_claim_epoch=1,
        owner="recovery-b",
        backend="local:recovery",
        lease_seconds=20,
        backend_absent_confirmed=True,
        now=now + timedelta(seconds=12),
    )

    assert resumed.claim == stranded
    assert resumed.status["claim_epoch"] == 2
    assert resumed.status["revision"] == running["revision"] + 1
