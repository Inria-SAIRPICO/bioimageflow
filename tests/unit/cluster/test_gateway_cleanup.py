from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import Any

import pytest

import bioimageflow.cluster.gateway as gateway_module
from bioimageflow.cluster.gateway import GatewayState, handle_request
from bioimageflow.cluster.protocol import GatewayRequest
from bioimageflow.launcher.schemas import new_run_id


def _request_plan(state: GatewayState, **filters: Any) -> dict[str, Any]:
    response = handle_request(state, GatewayRequest.create("plan-cleanup", filters))
    assert response.status == "ok"
    assert response.payload is not None
    return dict(response.payload)


def _request_apply(state: GatewayState, plan: dict[str, Any]):
    return handle_request(
        state,
        GatewayRequest.create(
            "apply-cleanup", {"plan": plan}, operation_id=plan["plan_id"]
        ),
    )


def _private_file(path: Path, content: bytes = b"payload") -> None:
    path.write_bytes(content)
    path.chmod(0o600)


def _retained_record(run_id: str, *, revision: int = 1) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "revision": revision,
        "phase": "submitted",
        "deployment_id": "sha256:" + "1" * 64,
        "object_id": "sha256:" + "2" * 64,
        "plan_digest": "sha256:" + "3" * 64,
        "launcher_bound": True,
    }


def test_cleanup_plan_is_a_non_mutating_exact_inventory_and_apply_is_idempotent(
    tmp_path: Path,
) -> None:
    state = GatewayState.initialize(tmp_path / "cluster-root")
    target = state.root / "objects" / ("a" * 64 + ".object")
    _private_file(target)

    plan = _request_plan(state, namespace="objects")

    assert target.exists()
    assert plan["schema"] == "bioimageflow.cluster_cleanup_plan.v1"
    assert len(plan["candidates"]) == 1
    candidate = plan["candidates"][0]
    assert candidate["path"] == f"objects/{target.name}"
    assert candidate["size"] == len(b"payload")
    assert candidate["reference_reasons"] == ()
    assert candidate["consequences"]

    first = _request_apply(state, plan)
    repeated = _request_apply(state, plan)

    assert first.status == repeated.status == "ok"
    assert first.payload == repeated.payload
    assert first.payload["removed"] == (candidate["identity"],)
    assert not target.exists()


def test_cleanup_skips_a_candidate_that_changed_after_confirmation(
    tmp_path: Path,
) -> None:
    state = GatewayState.initialize(tmp_path / "cluster-root")
    target = state.root / "objects" / ("a" * 64 + ".object")
    _private_file(target, b"first")
    plan = _request_plan(state, namespace="objects")
    _private_file(target, b"changed")

    response = _request_apply(state, plan)

    identity = plan["candidates"][0]["identity"]
    assert response.status == "ok"
    assert response.payload["removed"] == ()
    assert response.payload["skipped"] == {
        identity: "cleanup-conflict:candidate-changed"
    }
    assert target.read_bytes() == b"changed"


def test_cleanup_reference_revision_change_skips_every_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = GatewayState.initialize(tmp_path / "cluster-root")
    target = state.root / "objects" / ("a" * 64 + ".object")
    _private_file(target)
    records: list[dict[str, Any]] = []
    monkeypatch.setattr(state, "_retained_run_records", lambda: list(records))
    plan = _request_plan(state, namespace="objects")
    records.append(_retained_record(str(uuid.uuid4())))

    response = _request_apply(state, plan)

    identity = plan["candidates"][0]["identity"]
    assert response.payload["skipped"] == {
        identity: "cleanup-conflict:reference-revision-changed"
    }
    assert target.exists()


def test_retained_run_protects_its_deployment_and_uploaded_object(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = GatewayState.initialize(tmp_path / "cluster-root")
    record = _retained_record(str(uuid.uuid4()))
    deployment = state.root / "deployments" / record["deployment_id"][7:]
    deployment.mkdir(mode=0o700)
    _private_file(deployment / "publication.json", b"{}")
    uploaded = state.root / "objects" / f"{record['object_id'][7:]}.object"
    _private_file(uploaded)
    monkeypatch.setattr(state, "_retained_run_records", lambda: [record])

    deployment_plan = _request_plan(state, namespace="deployments")
    object_plan = _request_plan(state, namespace="objects")

    assert deployment_plan["candidates"] == ()
    assert object_plan["candidates"] == ()
    assert deployment.exists()
    assert uploaded.exists()


def test_run_cleanup_requires_explicit_terminal_selection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = GatewayState.initialize(tmp_path / "cluster-root")
    run_id = new_run_id()
    run = state.root / "runs" / run_id
    run.mkdir(mode=0o700)
    _private_file(run / "record.json", b"retained")
    record = _retained_record(run_id)
    monkeypatch.setattr(state, "_retained_run_records", lambda: [record])
    monkeypatch.setattr(state, "_run_is_terminal", lambda _record: True)

    unselected = _request_plan(state, namespace="runs")
    selected = _request_plan(state, namespace="runs", run_ids=[run_id])
    applied = _request_apply(state, selected)

    assert unselected["candidates"] == ()
    assert selected["candidates"][0]["reference_reasons"] == (
        "explicitly selected terminal run",
    )
    assert "attachment" in selected["candidates"][0]["consequences"][0]
    assert applied.status == "ok"
    assert not run.exists()


def test_run_cleanup_refuses_an_active_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = GatewayState.initialize(tmp_path / "cluster-root")
    run_id = new_run_id()
    (state.root / "runs" / run_id).mkdir(mode=0o700)
    record = _retained_record(run_id)
    monkeypatch.setattr(state, "_retained_run_records", lambda: [record])
    monkeypatch.setattr(state, "_run_is_terminal", lambda _record: False)

    response = handle_request(
        state,
        GatewayRequest.create(
            "plan-cleanup", {"namespace": "runs", "run_ids": [run_id]}
        ),
    )

    assert response.status == "error"
    assert response.diagnostic["category"] == "cleanup-conflict"
    assert (state.root / "runs" / run_id).exists()


def test_cleanup_refuses_symlink_candidates_and_preserves_the_target(
    tmp_path: Path,
) -> None:
    state = GatewayState.initialize(tmp_path / "cluster-root")
    outside = tmp_path / "outside"
    _private_file(outside)
    link = state.root / "objects" / ("a" * 64 + ".object")
    link.symlink_to(outside)

    response = handle_request(
        state,
        GatewayRequest.create("plan-cleanup", {"namespace": "objects"}),
    )

    assert response.status == "error"
    assert response.diagnostic["category"] == "cleanup-conflict"
    assert outside.read_bytes() == b"payload"
    assert os.path.islink(link)


def test_apply_rejects_a_forged_or_modified_cleanup_plan(tmp_path: Path) -> None:
    state = GatewayState.initialize(tmp_path / "cluster-root")
    target = state.root / "objects" / ("a" * 64 + ".object")
    _private_file(target)
    plan = _request_plan(state, namespace="objects")
    forged = {
        **plan,
        "candidates": [
            {
                **dict(plan["candidates"][0]),
                "path": "objects/" + "b" * 64 + ".object",
            }
        ],
    }

    response = _request_apply(state, forged)

    assert response.status == "error"
    assert response.diagnostic["category"] == "cleanup-conflict"
    assert target.exists()


def test_active_transfer_changes_the_reference_revision(
    tmp_path: Path,
) -> None:
    state = GatewayState.initialize(tmp_path / "cluster-root")
    target = state.root / "objects" / ("a" * 64 + ".object")
    _private_file(target)
    transfer = state.root / "transfers" / "active-transfer"
    transfer.mkdir(mode=0o700)
    lease = transfer / "lease.json"
    _private_file(lease, b"one")
    plan = _request_plan(state, namespace="objects")
    _private_file(lease, b"lease-revised")

    response = _request_apply(state, plan)

    identity = plan["candidates"][0]["identity"]
    assert response.payload["skipped"] == {
        identity: "cleanup-conflict:reference-revision-changed"
    }
    assert target.exists()


def test_replacement_race_is_detected_before_unlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = GatewayState.initialize(tmp_path / "cluster-root")
    target = state.root / "objects" / ("a" * 64 + ".object")
    _private_file(target, b"original")
    plan = _request_plan(state, namespace="objects")
    original_rename = gateway_module.os.rename
    raced = False

    def racing_rename(source: str, destination: str, **kwargs: Any) -> None:
        nonlocal raced
        if source == target.name and destination.endswith(".deleting") and not raced:
            raced = True
            source_descriptor = kwargs["src_dir_fd"]
            os.unlink(source, dir_fd=source_descriptor)
            descriptor = os.open(
                source,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
                dir_fd=source_descriptor,
            )
            try:
                os.write(descriptor, b"replacement")
            finally:
                os.close(descriptor)
        original_rename(source, destination, **kwargs)

    monkeypatch.setattr(gateway_module.os, "rename", racing_rename)

    response = _request_apply(state, plan)

    identity = plan["candidates"][0]["identity"]
    assert raced is True
    assert response.payload["skipped"] == {
        identity: "cleanup-conflict:candidate-replaced"
    }
    assert target.read_bytes() == b"replacement"


def test_apply_resumes_an_exact_tombstone_after_reference_revision_changes(
    tmp_path: Path,
) -> None:
    state = GatewayState.initialize(tmp_path / "cluster-root")
    target = state.root / "objects" / ("a" * 64 + ".object")
    _private_file(target)
    plan = _request_plan(state, namespace="objects")
    candidate = plan["candidates"][0]
    tombstone = state.root / "objects" / (
        f".cleanup-{plan['plan_id'][7:23]}-{candidate['identity'][7:23]}.deleting"
    )
    target.rename(tombstone)
    _private_file(state.root / "transfers" / "new-lease")

    response = _request_apply(state, plan)

    assert response.status == "ok"
    assert response.payload["removed"] == (candidate["identity"],)
    assert not tombstone.exists()


def test_cleanup_never_inventories_live_upload_slots(tmp_path: Path) -> None:
    state = GatewayState.initialize(tmp_path / "cluster-root")
    uploads = state.root / "temporary" / "uploads"
    uploads.mkdir(mode=0o700)
    partial = uploads / "active.partial"
    _private_file(partial)

    plan = _request_plan(
        state, namespace="temporary", older_than_seconds=86_400
    )
    unsafe = handle_request(
        state,
        GatewayRequest.create(
            "plan-cleanup",
            {"namespace": "temporary", "older_than_seconds": 0},
        ),
    )

    assert plan["candidates"] == ()
    assert unsafe.status == "error"
    assert unsafe.diagnostic["category"] == "protocol-incompatible"
    assert partial.exists()
