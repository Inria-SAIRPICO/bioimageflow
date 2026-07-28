from pathlib import Path

import pytest

from bioimageflow import Workflow
from bioimageflow.launcher.errors import LauncherProtocolError
from bioimageflow.launcher.payload import (
    load_workflow_payload,
    serialize_workflow_payload,
)
from bioimageflow.validation import ValidationError


def test_graph_payload_round_trip_uses_explicit_runtime_storage(tmp_path: Path) -> None:
    original_storage = tmp_path / "original"
    assigned_storage = tmp_path / "assigned"
    workflow = Workflow(storage_path=original_storage, engine="direct")

    payload = serialize_workflow_payload(workflow)
    restored = load_workflow_payload(payload, storage_path=assigned_storage)

    assert payload["kind"] == "graph_v1"
    assert "storage_path" not in payload["payload"]["config"]
    assert restored.storage_path == assigned_storage.absolute()


def test_payload_digest_fails_closed(tmp_path: Path) -> None:
    payload = serialize_workflow_payload(
        Workflow(storage_path=tmp_path / "storage", engine="direct")
    )
    payload["payload"]["name"] = "tampered"

    with pytest.raises(LauncherProtocolError, match="digest mismatch"):
        load_workflow_payload(payload, storage_path=tmp_path / "storage")


def test_partial_workflow_is_rejected_before_serialization(tmp_path: Path) -> None:
    workflow = Workflow(storage_path=tmp_path, engine="direct")
    workflow._build_errors = [
        ValidationError(kind="unknown_tool", message="missing")
    ]

    with pytest.raises(LauncherProtocolError, match="Partial workflows"):
        serialize_workflow_payload(workflow)


def test_unknown_payload_fields_are_rejected(tmp_path: Path) -> None:
    payload = serialize_workflow_payload(
        Workflow(storage_path=tmp_path, engine="direct")
    )
    payload["extra"] = True

    with pytest.raises(LauncherProtocolError, match="exactly"):
        load_workflow_payload(payload, storage_path=tmp_path)
