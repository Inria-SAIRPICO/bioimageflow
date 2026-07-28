from __future__ import annotations

import multiprocessing
import os
from pathlib import Path

import pytest

from bioimageflow.launcher.repository import (
    LauncherCorruptionError,
    LauncherRepository,
    RunAlreadyExistsError,
)
from bioimageflow.launcher.schemas import (
    SUBMISSION_SCHEMA,
    LauncherSchemaError,
    new_run_id,
    utc_timestamp,
    validate_run_id,
    validate_submission,
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
            "digest": "sha256:" + "a" * 64,
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


def _allocate_worker(
    storage_root: str,
    submission: dict[str, object],
    queue: multiprocessing.Queue,
) -> None:
    try:
        LauncherRepository(storage_root).allocate(submission, backend="local")
    except RunAlreadyExistsError:
        queue.put("collision")
    else:
        queue.put("created")


def test_run_ids_are_uuid4_values() -> None:
    run_id = new_run_id()

    assert validate_run_id(run_id) == run_id
    with pytest.raises(LauncherSchemaError, match="UUID4"):
        validate_run_id("run_00000000000010008000000000000000")
    with pytest.raises(LauncherSchemaError):
        validate_run_id("run_" + "A" * 32)


def test_schema_validation_rejects_unknown_and_missing_fields(
    tmp_path: Path,
) -> None:
    payload = _submission(tmp_path, new_run_id())
    payload["unknown"] = None

    with pytest.raises(LauncherSchemaError, match="unknown fields"):
        validate_submission(payload)

    del payload["unknown"]
    del payload["launch"]
    with pytest.raises(LauncherSchemaError, match="missing fields"):
        validate_submission(payload)


def test_candidate_inputs_install_with_control_metadata_atomically(
    tmp_path: Path,
) -> None:
    repository = LauncherRepository(tmp_path)
    run_id = repository.new_run_id()
    candidate = repository.create_candidate(run_id)
    inputs = candidate / "inputs"
    inputs.mkdir()
    (inputs / "root.parquet").write_bytes(b"parquet")

    assert not repository.run_control_path(run_id).exists()

    control = repository.allocate(
        _submission(repository.storage_root, run_id),
        backend="local",
        candidate_dir=candidate,
    )

    assert control.read_status()["state"] == "prepared"
    assert control.read_submission()["run_id"] == run_id
    assert control.progress_path.read_bytes() == b""
    assert control.confined_path(
        "inputs/root.parquet",
        must_exist=True,
    ).read_bytes() == b"parquet"
    assert not candidate.exists()


def test_allocation_rejects_control_and_canonical_collisions(
    tmp_path: Path,
) -> None:
    repository = LauncherRepository(tmp_path)
    run_id = repository.new_run_id()
    submission = _submission(repository.storage_root, run_id)
    repository.allocate(submission, backend="local")

    with pytest.raises(RunAlreadyExistsError):
        repository.allocate(submission, backend="local")

    other_run_id = repository.new_run_id()
    canonical = repository.canonical_run_path(other_run_id)
    canonical.mkdir(parents=True)
    with pytest.raises(RunAlreadyExistsError):
        repository.allocate(
            _submission(repository.storage_root, other_run_id),
            backend="local",
        )


def test_concurrent_allocation_has_one_winner(tmp_path: Path) -> None:
    repository = LauncherRepository(tmp_path)
    run_id = repository.new_run_id()
    submission = _submission(repository.storage_root, run_id)
    context = multiprocessing.get_context("spawn")
    queue = context.Queue()
    processes = [
        context.Process(
            target=_allocate_worker,
            args=(str(repository.storage_root), submission, queue),
        )
        for _ in range(2)
    ]

    for process in processes:
        process.start()
    results = [queue.get(timeout=15) for _ in processes]
    for process in processes:
        process.join(timeout=15)
        assert process.exitcode == 0

    assert sorted(results) == ["collision", "created"]
    assert repository.open(run_id).read_status()["revision"] == 0


@pytest.mark.skipif(
    not hasattr(os, "symlink"),
    reason="symlink support is required",
)
def test_control_paths_reject_symlink_components(tmp_path: Path) -> None:
    repository = LauncherRepository(tmp_path)
    run_id = repository.new_run_id()
    control = repository.allocate(
        _submission(repository.storage_root, run_id),
        backend="local",
    )
    outside = tmp_path / "outside"
    outside.mkdir()
    os.symlink(outside, control.control_dir / "escape")

    with pytest.raises(LauncherCorruptionError, match="Symlink"):
        control.confined_path("escape/value.json")


@pytest.mark.skipif(
    not hasattr(os, "symlink"),
    reason="symlink support is required",
)
def test_staged_candidates_reject_symlinks(tmp_path: Path) -> None:
    repository = LauncherRepository(tmp_path)
    run_id = repository.new_run_id()
    candidate = repository.create_candidate(run_id)
    outside = tmp_path / "external.parquet"
    outside.write_bytes(b"external")
    inputs = candidate / "inputs"
    inputs.mkdir()
    os.symlink(outside, inputs / "root.parquet")

    with pytest.raises(LauncherCorruptionError, match="symlink"):
        repository.allocate(
            _submission(repository.storage_root, run_id),
            backend="local",
            candidate_dir=candidate,
        )
    assert not candidate.exists()
