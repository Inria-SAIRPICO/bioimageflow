from __future__ import annotations

import multiprocessing
from pathlib import Path

import pytest

from bioimageflow.launcher.repository import (
    LauncherCorruptionError,
    LauncherRepository,
)
from bioimageflow.launcher.schemas import SUBMISSION_SCHEMA, utc_timestamp
from bioimageflow.launcher.state import (
    ClaimEpochMismatchError,
    InvalidTransitionError,
    RevisionConflictError,
)


def _submission(storage_root: Path, run_id: str) -> dict[str, object]:
    return {
        "schema": SUBMISSION_SCHEMA,
        "run_id": run_id,
        "created_at": utc_timestamp(),
        "storage_root": str(storage_root.resolve()),
        "canonical_view": f"views/runs/{run_id}",
        "workflow": {
            "kind": "graph",
            "digest": "sha256:" + "b" * 64,
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


def _progress_worker(
    storage_root: str,
    run_id: str,
    count: int,
) -> None:
    control = LauncherRepository(storage_root).open(run_id)
    for value in range(count):
        control.append_progress(
            kind="backend",
            payload={"schema": "test.backend.v1", "value": value},
        )


def _control(tmp_path: Path):
    repository = LauncherRepository(tmp_path)
    run_id = repository.new_run_id()
    return repository.allocate(
        _submission(repository.storage_root, run_id),
        backend="local",
    )


def test_exact_transitions_and_terminal_immutability(tmp_path: Path) -> None:
    control = _control(tmp_path)
    claimed = control.claim_start(
        expected_revision=0,
        owner="orchestrator-a",
        backend="local:123",
        lease_seconds=30,
    )
    running = control.transition(
        expected_revision=claimed.status["revision"],
        expected_claim_epoch=claimed.claim["epoch"],
        new_state="running",
    )
    finalizing = control.transition(
        expected_revision=running["revision"],
        expected_claim_epoch=claimed.claim["epoch"],
        new_state="finalizing",
    )
    succeeded = control.transition(
        expected_revision=finalizing["revision"],
        expected_claim_epoch=claimed.claim["epoch"],
        new_state="succeeded",
    )

    assert succeeded["revision"] == 4
    with pytest.raises(InvalidTransitionError):
        control.transition(
            expected_revision=succeeded["revision"],
            expected_claim_epoch=claimed.claim["epoch"],
            new_state="failed",
        )
    assert control.read_status() == succeeded


def test_revision_and_claim_epoch_are_compare_and_swap_guards(
    tmp_path: Path,
) -> None:
    control = _control(tmp_path)
    claimed = control.claim_start(
        expected_revision=0,
        owner="orchestrator-a",
        backend="local:123",
        lease_seconds=30,
    )

    with pytest.raises(RevisionConflictError):
        control.transition(
            expected_revision=0,
            expected_claim_epoch=claimed.claim["epoch"],
            new_state="running",
        )
    with pytest.raises(ClaimEpochMismatchError):
        control.transition(
            expected_revision=1,
            expected_claim_epoch=claimed.claim["epoch"] + 1,
            new_state="running",
        )
    assert control.read_status()["state"] == "starting"


def test_starting_cannot_bypass_atomic_execution_claim(tmp_path: Path) -> None:
    control = _control(tmp_path)

    with pytest.raises(InvalidTransitionError, match="claim_start"):
        control.transition(
            expected_revision=0,
            new_state="starting",
            updates={"claim_epoch": 1},
        )

    assert control.read_status()["state"] == "prepared"
    assert control.read_claim() is None


def test_prepared_and_running_cancellation_semantics(tmp_path: Path) -> None:
    prepared = _control(tmp_path / "prepared")
    cancelled = prepared.request_cancel(expected_revision=0)

    assert cancelled["state"] == "cancelled"
    assert cancelled["cancel_requested_at"] is not None
    assert not prepared.cancellation_marker_exists()
    assert prepared.request_cancel(expected_revision=1) == cancelled

    running_control = _control(tmp_path / "running")
    claimed = running_control.claim_start(
        expected_revision=0,
        owner="orchestrator-a",
        backend="local:123",
        lease_seconds=30,
    )
    running = running_control.transition(
        expected_revision=1,
        expected_claim_epoch=claimed.claim["epoch"],
        new_state="running",
    )
    requested = running_control.request_cancel(
        expected_revision=running["revision"],
        expected_claim_epoch=claimed.claim["epoch"],
    )

    assert requested["state"] == "cancel_requested"
    assert running_control.cancellation_marker_exists()
    with pytest.raises(InvalidTransitionError):
        running_control.transition(
            expected_revision=requested["revision"],
            expected_claim_epoch=claimed.claim["epoch"],
            new_state="finalizing",
        )


def test_finalizing_cancellation_is_a_no_op(tmp_path: Path) -> None:
    control = _control(tmp_path)
    claimed = control.claim_start(
        expected_revision=0,
        owner="orchestrator-a",
        backend="local:123",
        lease_seconds=30,
    )
    running = control.transition(
        expected_revision=1,
        expected_claim_epoch=1,
        new_state="running",
    )
    finalizing = control.transition(
        expected_revision=running["revision"],
        expected_claim_epoch=1,
        new_state="finalizing",
    )

    assert (
        control.request_cancel(
            expected_revision=finalizing["revision"],
            expected_claim_epoch=claimed.claim["epoch"],
        )
        == finalizing
    )
    assert not control.cancellation_marker_exists()


def test_progress_sequence_is_global_across_processes(tmp_path: Path) -> None:
    control = _control(tmp_path)
    context = multiprocessing.get_context("spawn")
    processes = [
        context.Process(
            target=_progress_worker,
            args=(str(tmp_path.resolve()), control.run_id, 15),
        )
        for _ in range(3)
    ]

    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=20)
        assert process.exitcode == 0

    entries = control.read_progress()
    assert [entry["sequence"] for entry in entries] == list(range(1, 46))


def test_progress_recovers_one_unterminated_tail(tmp_path: Path) -> None:
    control = _control(tmp_path)
    first = control.append_progress(
        kind="public",
        payload={"schema": "test.public.v1"},
    )
    with control.progress_path.open("ab") as stream:
        stream.write(b'{"schema":"incomplete"')

    assert control.read_progress() == [first]
    second = control.append_progress(
        kind="backend",
        payload={"schema": "test.backend.v1"},
    )

    assert second["sequence"] == 2
    assert [entry["sequence"] for entry in control.read_progress()] == [1, 2]


def test_progress_rejects_malformed_complete_lines(tmp_path: Path) -> None:
    control = _control(tmp_path)
    control.progress_path.write_bytes(b'{"schema":"broken"}\n')

    with pytest.raises(LauncherCorruptionError, match="Invalid progress"):
        control.read_progress()
