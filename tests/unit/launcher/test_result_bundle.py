from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from bioimageflow import (
    ExecutorBinding,
    ExecutorCapabilities,
    OrchestratorLaunchConfig,
    ParslConfigRef,
    WorkerEnvironmentAttestation,
    WorkerSlotCapacity,
    Workflow,
    submit_workflow,
)
from bioimageflow.launcher.cluster_protocol import ClusterProtocolFailure
from bioimageflow.launcher.orchestrator import run_orchestrator
from bioimageflow.launcher.remote_control import read_log_page, read_progress_page
from bioimageflow.launcher.result_bundle import prepare_result
from bioimageflow.launcher.result_download import (
    SSHTransportError,
    _load_manifest,
    _validate_entries,
    _verify_tree,
)
from bioimageflow.launcher.returns import load_public_return_from_bundle


def _binding() -> ExecutorBinding:
    return ExecutorBinding(
        label="threads",
        environments=(
            WorkerEnvironmentAttestation(
                name="default",
                dependency_hash="0" * 64,
                allow_flexible_versions=False,
                core_requirement="bioimageflow-core>=0.2.0,<0.3",
            ),
        ),
        capabilities=ExecutorCapabilities(
            storage_modes=("shared_fs",),
            tool_origin_modes=("installed_module",),
            slot=WorkerSlotCapacity(cpu=1),
        ),
    )


def _successful_run(storage: Path):
    run = submit_workflow(
        Workflow(storage_path=storage, engine="direct"),
        parsl_config=ParslConfigRef(
            "tests.unit.launcher.config_factories:build",
            {"workers": 1},
        ),
        executor_bindings={"threads": _binding()},
        launch=OrchestratorLaunchConfig(backend="manual"),
    )
    assert run_orchestrator(storage, run.id, lease_seconds=2, poll_seconds=0.01) == (
        "succeeded"
    )
    return run


def test_prepare_result_builds_reusable_self_validating_bundle(
    tmp_path: Path,
) -> None:
    storage = tmp_path / "storage"
    staging = tmp_path / "transport"
    run = _successful_run(storage)
    request_id = "12345678-1234-4abc-9234-56789abcdef0"

    first = prepare_result(
        staging.as_posix(),
        storage.as_posix(),
        run.id,
        request_id,
        "sha256:" + "1" * 64,
    )
    second = prepare_result(
        staging.as_posix(),
        storage.as_posix(),
        run.id,
        request_id,
        "sha256:" + "1" * 64,
    )

    assert first == second
    root = Path(first["remote_root"])
    manifest = _load_manifest(root / "manifest.json")
    record_assets = _verify_tree(root, manifest)
    result = load_public_return_from_bundle(
        root,
        manifest["return_manifest"],
        record_assets,
    )
    assert isinstance(result, pd.DataFrame)
    assert result.empty
    paths = {entry["path"] for entry in manifest["entries"]}
    assert not any(path.startswith(("launcher/", "views/", "results/")) for path in paths)


def test_prepare_result_rejects_nonterminal_and_overlapping_staging(
    tmp_path: Path,
) -> None:
    storage = tmp_path / "storage"
    run = submit_workflow(
        Workflow(storage_path=storage, engine="direct"),
        parsl_config=ParslConfigRef(
            "tests.unit.launcher.config_factories:build",
            {"workers": 1},
        ),
        executor_bindings={"threads": _binding()},
        launch=OrchestratorLaunchConfig(backend="manual"),
    )

    with pytest.raises(ClusterProtocolFailure, match="not succeeded"):
        prepare_result(
            (tmp_path / "transport").as_posix(),
            storage.as_posix(),
            run.id,
            "12345678-1234-4abc-9234-56789abcdef0",
            "sha256:" + "2" * 64,
        )
    with pytest.raises(ClusterProtocolFailure, match="disjoint"):
        prepare_result(
            (storage / "transport").as_posix(),
            storage.as_posix(),
            run.id,
            "22345678-1234-4abc-9234-56789abcdef0",
            "sha256:" + "3" * 64,
        )


def test_bundle_digest_corruption_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps({"schema": "wrong"}))

    with pytest.raises(Exception, match="schema"):
        _load_manifest(path)


def test_result_manifest_rejects_duplicate_json_keys(tmp_path: Path) -> None:
    path = tmp_path / "manifest.json"
    path.write_text('{"schema":"one","schema":"two"}')

    with pytest.raises(SSHTransportError, match="malformed"):
        _load_manifest(path)


def test_result_entries_reject_nonportable_path_collisions() -> None:
    digest = "sha256:" + "0" * 64
    entries = [
        {"digest": digest, "kind": "file", "path": "A.txt", "size": 0},
        {"digest": digest, "kind": "file", "path": "a.txt", "size": 0},
    ]

    with pytest.raises(SSHTransportError, match="invalid"):
        _validate_entries(entries)


def test_remote_progress_page_is_bounded_and_preserves_sequences(
    tmp_path: Path,
) -> None:
    storage = tmp_path / "storage"
    run = _successful_run(storage)
    existing = run._control.read_progress()
    after = existing[-1]["sequence"] if existing else 0
    run._control.append_progress(
        kind="backend",
        payload={
            "event": "orchestrator_running",
            "owner": "review-one",
            "schema": "bioimageflow.launcher.backend_event.v1",
        },
    )
    run._control.append_progress(
        kind="backend",
        payload={
            "event": "orchestrator_succeeded",
            "owner": "review-two",
            "schema": "bioimageflow.launcher.backend_event.v1",
        },
    )

    first = read_progress_page(storage.as_posix(), run.id, after, 1)
    second = read_progress_page(
        storage.as_posix(),
        run.id,
        first["next_sequence"],
        1,
    )

    assert [event["sequence"] for event in first["events"]] == [after + 1]
    assert first["has_more"] is True
    assert [event["sequence"] for event in second["events"]] == [after + 2]
    assert second["has_more"] is False


def test_remote_log_pages_hold_the_first_page_snapshot(tmp_path: Path) -> None:
    storage = tmp_path / "storage"
    run = _successful_run(storage)
    logs = run.control_dir / "logs"
    logs.mkdir(exist_ok=True)
    stdout = logs / "orchestrator.out"
    stdout.write_bytes(b"abc")

    first = read_log_page(
        storage.as_posix(),
        run.id,
        "stdout",
        0,
        None,
        None,
        2,
    )
    stdout.write_bytes(b"abcdef")
    second = read_log_page(
        storage.as_posix(),
        run.id,
        "stdout",
        first["next_offset"],
        first["identity"],
        first["snapshot_size"],
        2,
    )

    assert first["snapshot_size"] == 3
    assert second["snapshot_size"] == 3
    assert second["eof"] is True
    assert second["next_offset"] == 3
