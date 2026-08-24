from __future__ import annotations

import hashlib
import json
import uuid
import zipfile
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

import bioimageflow.cluster._gateway_run_state as gateway_run_module
from bioimageflow.cluster._gateway_archive import _extract_invocation_archive
from bioimageflow.cluster._gateway_support import GatewayOperationFailure
from bioimageflow.cluster.gateway import GatewayState, handle_request
from bioimageflow.cluster.plan import RemoteExecutionPlan
from bioimageflow.cluster.protocol import GatewayRequest
from bioimageflow.cluster.values import SchedulerJob
from bioimageflow.launcher.schemas import new_run_id
from bioimageflow.parsl import ParslTaskPolicy


def _invocation_archive(tmp_path: Path) -> tuple[Path, dict[str, Any], str]:
    content = b"{}"
    entry = {
        "path": "invocation.json",
        "kind": "file",
        "size": len(content),
        "digest": f"sha256:{hashlib.sha256(content).hexdigest()}",
    }
    invocation_digest = "sha256:" + "4" * 64
    manifest = {
        "schema": "bioimageflow.prepared_cluster_invocation.v1",
        "invocation_digest": invocation_digest,
        "workflow_digest": "sha256:" + "5" * 64,
        "entries": [entry],
        "inputs": [],
        "targets": None,
        "node_input_overrides": [],
        "task_policy": ParslTaskPolicy().to_dict(),
    }
    archive = tmp_path / "invocation.zip"
    with zipfile.ZipFile(archive, "x") as output:
        output.writestr("invocation.json", content)
    return archive, manifest, invocation_digest


def _plan(
    root: Path,
    invocation_digest: str,
    *,
    scheduler_job: SchedulerJob | None = None,
) -> RemoteExecutionPlan:
    return RemoteExecutionPlan(
        host="cluster",
        cluster_root=str(root),
        attempt_id=str(uuid.uuid4()),
        run_id=new_run_id(),
        deployment_id="sha256:" + "1" * 64,
        external_attestation_digest="sha256:" + "2" * 64,
        invocation_digest=invocation_digest,
        validation_digest="sha256:" + "3" * 64,
        validation_expires_at="2099-01-01T00:00:00Z",
        validation_evidence={},
        executor_claims={},
        storage_path=str(root.parent / "workflow-storage"),
        expires_at="2099-01-01T00:00:00Z",
        scheduler_job=scheduler_job or SchedulerJob("slurm", timedelta(minutes=5)),
        task_policy=ParslTaskPolicy(),
        nodes=(),
    )


def _submit_request(
    plan: RemoteExecutionPlan, manifest: dict[str, Any], archive: Path
) -> GatewayRequest:
    digest = f"sha256:{hashlib.sha256(archive.read_bytes()).hexdigest()}"
    return GatewayRequest.create(
        "submit-plan",
        {
            "plan": plan.to_dict(),
            "invocation_manifest": manifest,
            "object_id": digest,
            "object_size": archive.stat().st_size,
        },
        operation_id=plan.attempt_id,
        payload_digest=digest,
    )


@pytest.mark.parametrize("case", ["tampered", "missing", "extra"])
def test_invocation_extraction_rejects_archive_inventory_drift(
    tmp_path: Path, case: str
) -> None:
    expected = b"{}"
    entries = [
        {
            "path": "invocation.json",
            "kind": "file",
            "size": len(expected),
            "digest": f"sha256:{hashlib.sha256(expected).hexdigest()}",
        }
    ]
    archive = tmp_path / f"{case}.zip"
    with zipfile.ZipFile(archive, "x") as output:
        if case != "missing":
            output.writestr(
                "invocation.json", b"changed" if case == "tampered" else expected
            )
        if case == "extra":
            output.writestr("undeclared.txt", b"extra")

    with pytest.raises(GatewayOperationFailure) as failure:
        _extract_invocation_archive(
            archive, tmp_path / f"destination-{case}", {"entries": entries}
        )

    assert failure.value.diagnostic["category"] == "deployment-tampered"
    assert not (tmp_path / f"destination-{case}").exists()


def test_submit_allocates_attachable_run_before_upload_and_cancel_seals_it(
    tmp_path: Path,
) -> None:
    state = GatewayState.initialize(tmp_path / "cluster-root")
    archive, manifest, invocation_digest = _invocation_archive(tmp_path)
    plan = _plan(state.root, invocation_digest)
    request = _submit_request(plan, manifest, archive)

    first = handle_request(state, request)
    repeated = handle_request(state, request)

    assert first.status == repeated.status == "ok"
    assert first.payload["upload_required"] is True
    assert repeated.payload == first.payload
    assert first.payload["observation"]["attempt_phase"] == "uploading"
    assert first.payload["observation"]["gateway_publication_id"]
    assert first.payload["observation"]["gateway_artifact_digest"].startswith(
        "sha256:"
    )
    assert state.inspect_run({"run_id": plan.run_id})["state"] == "prepared"

    cancelled = handle_request(
        state,
        GatewayRequest.create(
            "cancel-run",
            {"run_id": plan.run_id},
            operation_id=str(uuid.uuid4()),
        ),
    )
    assert cancelled.payload["state"] == "cancelled"
    final = handle_request(state, request)
    assert final.payload["upload_required"] is False
    assert final.payload["observation"]["state"] == "cancelled"


def test_run_record_rejects_malformed_plan_and_gateway_binding_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = GatewayState.initialize(tmp_path / "cluster-root")
    archive, manifest, invocation_digest = _invocation_archive(tmp_path)
    plan = _plan(state.root, invocation_digest)
    request = _submit_request(plan, manifest, archive)
    handle_request(state, request)
    record_path = state.root / "runs" / plan.run_id / "record.json"
    record = json.loads(record_path.read_bytes())
    record["plan"] = []
    record_path.write_text(json.dumps(record))
    record_path.chmod(0o600)

    malformed = handle_request(
        state,
        GatewayRequest.create("inspect-run", {"run_id": plan.run_id}),
    )

    assert malformed.status == "error"
    assert malformed.diagnostic["category"] == "operation-record-tampered"

    record["plan"] = plan.to_dict()
    record_path.write_text(json.dumps(record))
    record_path.chmod(0o600)
    monkeypatch.setenv("BIOIMAGEFLOW_GATEWAY_PUBLICATION_ID", "other-publication")
    drifted = handle_request(
        state,
        GatewayRequest.create("inspect-run", {"run_id": plan.run_id}),
    )

    assert drifted.status == "error"
    assert drifted.diagnostic["category"] == "gateway-untrusted"


@pytest.mark.parametrize(
    "scheduler_job",
    [
        SchedulerJob("slurm", timedelta(minutes=5), gpu=1),
        SchedulerJob("slurm", timedelta(minutes=5), memory="1 GiB"),
        SchedulerJob(
            "slurm", timedelta(minutes=5), attributes={"exclusive": True}
        ),
    ],
)
def test_submit_rejects_unsupported_scheduler_resources_before_run_allocation(
    tmp_path: Path, scheduler_job: SchedulerJob
) -> None:
    state = GatewayState.initialize(tmp_path / "cluster-root")
    archive, manifest, invocation_digest = _invocation_archive(tmp_path)
    plan = _plan(
        state.root, invocation_digest, scheduler_job=scheduler_job
    )

    response = handle_request(state, _submit_request(plan, manifest, archive))

    assert response.status == "error"
    assert response.diagnostic["category"] == "unsupported-scheduler-adapter"
    assert not (state.root / "runs" / plan.run_id).exists()


def test_committed_invocation_submits_once_and_reuses_scheduler_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = GatewayState.initialize(tmp_path / "cluster-root")
    archive, manifest, invocation_digest = _invocation_archive(tmp_path)
    plan = _plan(state.root, invocation_digest)
    request = _submit_request(plan, manifest, archive)
    first = handle_request(state, request)
    assert first.payload["upload_required"] is True
    object_path = state.root / "objects" / f"{request.payload_digest[7:]}.object"
    object_path.write_bytes(archive.read_bytes())
    object_path.chmod(0o600)
    deployment = tmp_path / "deployment"
    deployment.mkdir()
    (deployment / "content" / "parsl").mkdir(parents=True)
    (deployment / "activation.sh").write_text("true\n")
    (deployment / "submit_run.py").write_text("# fixed\n")
    runtime = (
        deployment,
        {
            "parsl": {
                "source_kind": "file",
                "source": None,
                "factory": "build",
                "kwargs": {},
                "secret_refs": {},
            }
        },
        {"requested_executable": "/usr/bin/python3"},
        {
            "executor_bindings": {},
            "evidence": {},
            "expires_at": "2099-01-01T00:00:00Z",
        },
    )
    monkeypatch.setattr(state, "_deployment_runtime", lambda *_args, **_kwargs: runtime)
    submissions: list[list[str]] = []

    def fake_child(argv: list[str], **_kwargs: Any) -> dict[str, Any]:
        submissions.append(argv)
        return {
            "status": "ok",
            "payload": {
                "schema": "bioimageflow.launcher.run-observation.v1",
                "error": None,
                "retry_plan": None,
                "run_id": plan.run_id,
                "state": "starting",
                "status_revision": 1,
                "storage_path": plan.storage_path,
                "terminal": False,
                "updated_at": "2026-01-01T00:00:00Z",
            },
        }

    monkeypatch.setattr(gateway_run_module, "_run_json_child", fake_child)
    submitted = handle_request(state, request)
    repeated = handle_request(state, request)

    assert submitted.status == repeated.status == "ok"
    assert submitted.payload["observation"]["state"] == "starting"
    assert repeated.payload["observation"]["run_id"] == plan.run_id
    assert sum(Path(argv[3]).name == "submit_run.py" for argv in submissions) == 1


def test_attach_lifecycle_delegates_only_through_retained_run_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = GatewayState.initialize(tmp_path / "cluster-root")
    archive, manifest, invocation_digest = _invocation_archive(tmp_path)
    plan = _plan(state.root, invocation_digest)
    request = _submit_request(plan, manifest, archive)
    handle_request(state, request)
    record = state._read_run_record(plan.run_id)
    record = {**record, "phase": "submitted", "launcher_bound": True, "revision": 2}
    state._write_run_record(record)
    calls: list[tuple[str, dict[str, Any]]] = []

    def controller(
        _record: dict[str, Any], operation: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        calls.append((operation, arguments))
        if operation == "read-progress":
            return {"events": [], "has_more": False, "next_sequence": 7}
        if operation == "plan-retry":
            return {"schema": "test.retry.v1"}
        if operation == "prepare-result":
            return {"bundle_digest": "sha256:" + "9" * 64}
        return {"run_id": plan.run_id, "state": "running"}

    monkeypatch.setattr(state, "_run_controller", controller)

    assert state.inspect_run({"run_id": plan.run_id})["state"] == "running"
    state.refresh_run({"run_id": plan.run_id})
    assert state.read_progress(
        {"run_id": plan.run_id, "after_sequence": 7, "limit": 50}
    )["next_sequence"] == 7
    state.cancel_run({"run_id": plan.run_id})
    state.plan_retry({"run_id": plan.run_id, "recompute": None})
    state.prepare_result({"run_id": plan.run_id})

    assert [operation for operation, _arguments in calls] == [
        "inspect-run",
        "refresh-run",
        "read-progress",
        "cancel-run",
        "plan-retry",
        "prepare-result",
    ]
