from __future__ import annotations

import json
import hashlib
import shutil
from concurrent.futures import ThreadPoolExecutor
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
    WorkflowExecutionContext,
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
from bioimageflow.storage import canonical_json_bytes


def _binding() -> ExecutorBinding:
    return ExecutorBinding(
        label="threads",
        environments=(
            WorkerEnvironmentAttestation(
                name="default",
                dependency_hash="0" * 64,
                allow_flexible_versions=False,
                core_requirement="bioimageflow-core>=0.2.1,<0.3",
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


def test_local_run_result_destination_is_atomic_and_idempotent(tmp_path: Path) -> None:
    storage = tmp_path / "storage"
    run = _successful_run(storage)
    destination = tmp_path / "download"

    first = run.result(destination=destination)
    shutil.rmtree(run.control_dir / "return")
    second = run.result(destination=destination)

    assert isinstance(first, pd.DataFrame) and first.empty
    assert isinstance(second, pd.DataFrame) and second.empty
    manifest = _load_manifest(destination / "manifest.json")
    assert manifest["run_id"] == run.id
    (destination / "manifest.json").write_text("{}")
    with pytest.raises(SSHTransportError, match="manifest"):
        run.result(destination=destination)


def test_local_export_rejects_a_different_self_consistent_bundle(
    tmp_path: Path,
) -> None:
    storage = tmp_path / "storage"
    run = _successful_run(storage)
    destination = tmp_path / "download"
    run.result(destination=destination)
    manifest_path = destination / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    forged = destination / "z-forged"
    forged.mkdir()
    manifest["entries"].append(
        {
            "digest": f"sha256:{hashlib.sha256(b'').hexdigest()}",
            "kind": "directory",
            "path": "z-forged",
            "size": 0,
        }
    )
    body = {key: value for key, value in manifest.items() if key != "digest"}
    manifest["digest"] = (
        f"sha256:{hashlib.sha256(canonical_json_bytes(body)).hexdigest()}"
    )
    manifest_path.write_text(json.dumps(manifest, sort_keys=True))

    with pytest.raises(FileExistsError, match="another bundle"):
        run.result(destination=destination)


def test_attached_context_exports_the_same_result_bundle(tmp_path: Path) -> None:
    workflow = Workflow(storage_path=tmp_path / "storage", engine="direct")
    context = WorkflowExecutionContext()
    result = workflow.compute(run_context=context)
    destination = tmp_path / "attached-download"

    exported = context.export_result(result, destination=destination)
    repeated = context.export_result(result, destination=destination)

    assert isinstance(exported, pd.DataFrame) and exported.empty
    assert isinstance(repeated, pd.DataFrame) and repeated.empty
    manifest = _load_manifest(destination / "manifest.json")
    assert manifest["run_id"] == context.run_id
    result["changed"] = pd.Series(dtype="int64")
    conflicting = tmp_path / "attached-conflict"
    with pytest.raises(SSHTransportError, match="identity"):
        context.export_result(result, destination=conflicting)
    assert not conflicting.exists()


def test_attached_context_claims_one_identity_across_concurrent_exports(
    tmp_path: Path,
) -> None:
    workflow = Workflow(storage_path=tmp_path / "storage", engine="direct")
    context = WorkflowExecutionContext()
    result = workflow.compute(run_context=context)
    changed = result.assign(changed=pd.Series(dtype="int64"))
    destinations = (tmp_path / "first", tmp_path / "second")

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = tuple(
            executor.submit(context.export_result, value, destination=destination)
            for value, destination in zip((result, changed), destinations, strict=True)
        )
        outcomes = []
        for future in futures:
            try:
                outcomes.append(future.result())
            except SSHTransportError as error:
                outcomes.append(error)

    assert sum(isinstance(value, SSHTransportError) for value in outcomes) == 1
    assert sum(path.exists() for path in destinations) == 1


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
