from __future__ import annotations

import os
import hashlib
import json
import uuid
import zipfile
from pathlib import Path

import pytest

from bioimageflow.cluster.gateway import (
    ROOT_NAMESPACES,
    GatewayOperationFailure,
    GatewayState,
    handle_request,
)
from bioimageflow.cluster.protocol import GatewayRequest


def test_gateway_initializes_private_namespaces(tmp_path: Path) -> None:
    root = tmp_path / "cluster-root"
    state = GatewayState.initialize(root)

    assert state.root == root
    assert {child.name for child in root.iterdir()} == set(ROOT_NAMESPACES)
    assert os.stat(root).st_mode & 0o777 == 0o700
    assert all(os.stat(root / name).st_mode & 0o777 == 0o700 for name in ROOT_NAMESPACES)


def test_mutation_receipt_returns_same_result_without_repeating_handler(
    tmp_path: Path,
) -> None:
    state = GatewayState.initialize(tmp_path / "cluster-root")
    operation_id = str(uuid.uuid4())
    calls = 0

    def handler(arguments):
        nonlocal calls
        calls += 1
        return {"run_id": arguments["run_id"], "state": "starting"}

    first = GatewayRequest.create(
        "submit_plan",
        {"run_id": "run-1"},
        operation_id=operation_id,
    )
    retry = GatewayRequest.create(
        "submit_plan",
        {"run_id": "run-1"},
        operation_id=operation_id,
    )

    assert handle_request(state, first, {"submit_plan": handler}).status == "ok"
    assert handle_request(state, retry, {"submit_plan": handler}).status == "ok"
    assert calls == 1
    receipt = state.root / "operations" / f"{operation_id}.json"
    assert receipt.stat().st_mode & 0o777 == 0o600


def test_operation_id_conflict_fails_without_mutation(tmp_path: Path) -> None:
    state = GatewayState.initialize(tmp_path / "cluster-root")
    operation_id = str(uuid.uuid4())
    handlers = {"submit_plan": lambda arguments: {"run_id": arguments["run_id"]}}
    first = GatewayRequest.create(
        "submit_plan", {"run_id": "one"}, operation_id=operation_id
    )
    conflicting = GatewayRequest.create(
        "submit_plan", {"run_id": "two"}, operation_id=operation_id
    )

    assert handle_request(state, first, handlers).status == "ok"
    response = handle_request(state, conflicting, handlers)

    assert response.status == "error"
    assert response.diagnostic["category"] == "operation-conflict"


def test_tampered_receipt_fails_closed(tmp_path: Path) -> None:
    state = GatewayState.initialize(tmp_path / "cluster-root")
    operation_id = str(uuid.uuid4())
    request = GatewayRequest.create("mutate", {}, operation_id=operation_id)
    handle_request(state, request, {"mutate": lambda arguments: {"ok": True}})
    receipt = state.root / "operations" / f"{operation_id}.json"
    receipt.chmod(0o644)

    with pytest.raises(GatewayOperationFailure) as captured:
        state.mutate(request, lambda arguments: {"ok": False})

    assert captured.value.diagnostic["category"] == "operation-record-tampered"


def test_gateway_rejects_symlinked_managed_namespace(tmp_path: Path) -> None:
    root = tmp_path / "cluster-root"
    state = GatewayState.initialize(root)
    target = tmp_path / "outside"
    target.mkdir()
    (root / "objects").rmdir()
    (root / "objects").symlink_to(target, target_is_directory=True)

    with pytest.raises(GatewayOperationFailure):
        state.validate_layout()


def test_gateway_commits_object_and_publishes_uninstalled_deployment(
    tmp_path: Path,
) -> None:
    state = GatewayState.initialize(tmp_path / "cluster-root")
    deployment_id = "sha256:" + "1" * 64
    manifest_digest = "sha256:" + "2" * 64
    archive = tmp_path / "deployment.zip"
    with zipfile.ZipFile(archive, "x") as output:
        output.writestr(
            "deployment-manifest.json",
            json.dumps(
                {
                    "deployment_id": deployment_id,
                    "manifest_digest": manifest_digest,
                }
            ),
        )
    content = archive.read_bytes()
    digest = f"sha256:{hashlib.sha256(content).hexdigest()}"
    allocate = GatewayRequest.create(
        "allocate_upload",
        {"size": len(content), "digest": digest, "kind": "deployment"},
        operation_id=str(uuid.uuid4()),
        payload_digest=digest,
    )
    allocation = handle_request(state, allocate).payload
    Path(allocation["upload_path"]).write_bytes(content)
    Path(allocation["upload_path"]).chmod(0o600)
    commit = GatewayRequest.create(
        "commit_upload",
        {
            "upload_token": allocation["upload_token"],
            "size": len(content),
            "digest": digest,
        },
        operation_id=str(uuid.uuid4()),
        payload_digest=digest,
    )
    committed = handle_request(state, commit)
    assert committed.payload["object_id"] == digest
    publish = GatewayRequest.create(
        "publish_deployment",
        {
            "object_id": digest,
            "deployment_id": deployment_id,
            "manifest_digest": manifest_digest,
        },
        operation_id=str(uuid.uuid4()),
        payload_digest=digest,
    )

    published = handle_request(state, publish)

    assert published.status == "ok"
    assert published.payload["state"] == "published"
    assert published.payload["environment_installed"] is False
    validation = GatewayRequest.create(
        "validate-deployment",
        {
            "deployment_id": deployment_id,
            "scheduler_job": {},
            "timeout": None,
        },
    )
    refused = handle_request(state, validation)
    assert refused.status == "error"
    assert refused.diagnostic["category"] == "deployment-install-failed"
