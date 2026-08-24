from __future__ import annotations

import json
from pathlib import Path

import pytest

from bioimageflow.cluster import preparation
from bioimageflow.cluster.preparation import (
    PreparedClusterInvocation,
    prepare_cluster_invocation,
)
from bioimageflow.launcher.types import LocalUpload
from bioimageflow.workflow import Workflow


def _workflow_with_path_input() -> Workflow:
    workflow = Workflow(name="storage-free")
    workflow.input("source", Path)
    return workflow


def test_workflow_definition_can_be_storage_free_and_prepared() -> None:
    workflow = Workflow(name="storage-free")

    assert workflow.storage_path is None
    assert "storage_path" not in workflow.to_dict()["config"]
    with prepare_cluster_invocation(workflow) as prepared:
        assert prepared.root.is_dir()
    with pytest.raises(RuntimeError, match="runtime storage_path"):
        workflow.compute()


def test_preparation_owns_an_immutable_upload_snapshot(tmp_path) -> None:
    source = tmp_path / "laptop-input.txt"
    source.write_bytes(b"first version")
    prepared = prepare_cluster_invocation(
        _workflow_with_path_input(),
        inputs={"source": LocalUpload(source)},
    )
    snapshot = prepared.root / "uploads" / "0" / source.name
    public_json = json.dumps(prepared.to_dict())

    source.write_bytes(b"second version")
    source.unlink()

    assert snapshot.read_bytes() == b"first version"
    assert str(source) not in public_json
    prepared._verify()
    snapshot.write_bytes(b"tampered")
    with pytest.raises(RuntimeError, match="no longer match"):
        prepared._verify()
    prepared.close()


def test_prepared_manifest_is_deeply_immutable(tmp_path) -> None:
    source = tmp_path / "input.txt"
    source.write_text("input", encoding="utf-8")
    prepared = prepare_cluster_invocation(
        _workflow_with_path_input(),
        inputs={"source": LocalUpload(source)},
    )

    with pytest.raises(TypeError):
        prepared.manifest.inputs[0]["name"] = "changed"  # type: ignore[index]
    exported = prepared.to_dict()
    exported["inputs"][0]["name"] = "changed"
    assert prepared.to_dict()["inputs"][0]["name"] == "source"
    prepared.close()


def test_from_dict_is_a_detached_summary_and_never_uses_original_paths(tmp_path) -> None:
    source = tmp_path / "input.txt"
    source.write_text("input", encoding="utf-8")
    owner = prepare_cluster_invocation(
        _workflow_with_path_input(),
        inputs={"source": LocalUpload(source)},
    )
    payload = owner.to_dict()
    owner.close()
    source.unlink()

    summary = PreparedClusterInvocation.from_dict(payload)

    assert summary.detached
    assert summary.to_dict() == payload
    with pytest.raises(RuntimeError, match="local-state-unavailable"):
        _ = summary.root
    with pytest.raises(RuntimeError, match="local-state-unavailable"):
        summary._acquire_lease("plan-1")


def test_one_plan_lease_protects_bytes_from_close_and_expiry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = [100.0]
    monkeypatch.setattr(preparation.time, "monotonic", lambda: clock[0])
    prepared = prepare_cluster_invocation(Workflow(name="leased"), lifetime=1)
    prepared._acquire_lease("plan-1")
    clock[0] = 1000.0

    assert prepared.expired
    assert prepared.root.is_dir()
    with pytest.raises(RuntimeError, match="leased by an active plan"):
        prepared.close()
    with pytest.raises(RuntimeError, match="active plan"):
        prepared._acquire_lease("plan-2")

    prepared._release_lease("plan-1")
    with pytest.raises(RuntimeError, match="expired"):
        _ = prepared.root
    assert prepared.closed


def test_close_is_idempotent_and_removes_only_owned_snapshot(tmp_path) -> None:
    source = tmp_path / "input.txt"
    source.write_text("original", encoding="utf-8")
    prepared = prepare_cluster_invocation(
        _workflow_with_path_input(),
        inputs={"source": LocalUpload(source)},
    )
    owned_root = prepared.root

    prepared.close()
    prepared.close()

    assert not owned_root.exists()
    assert source.read_text(encoding="utf-8") == "original"
    with pytest.raises(RuntimeError, match="local-state-unavailable"):
        _ = prepared.root


def test_prepared_summary_rejects_unknown_serialized_fields() -> None:
    prepared = prepare_cluster_invocation(Workflow(name="strict"))
    payload = prepared.to_dict()
    prepared.close()

    with pytest.raises(ValueError, match="Invalid PreparedInvocationManifest"):
        PreparedClusterInvocation.from_dict({**payload, "unknown": None})


@pytest.mark.parametrize("lifetime", [0, -1, float("nan"), float("inf")])
def test_preparation_lifetime_must_be_positive_and_finite(lifetime: float) -> None:
    with pytest.raises(ValueError, match="positive finite"):
        prepare_cluster_invocation(Workflow(name="strict-lifetime"), lifetime=lifetime)
