from pathlib import Path

import pandas as pd
import pytest

from bioimageflow import DataFrameTool, Workflow
from bioimageflow.launcher.errors import LauncherProtocolError
from bioimageflow.launcher.payload import (
    load_workflow_payload,
    serialize_workflow_payload,
)
from bioimageflow.validation import ValidationError
from bioimageflow_core import IOModel


class _ArchiveSource(DataFrameTool):
    accepts_upstream = False

    class Outputs(IOModel):
        value: int

    def transform(self, df, arguments):
        return pd.DataFrame({"value": [1]})


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


def test_archive_payload_includes_custom_source_once_and_round_trips(
    tmp_path: Path,
) -> None:
    workflow = Workflow(storage_path=tmp_path / "original", engine="direct")
    with workflow:
        _ArchiveSource()(name="source")

    payload = serialize_workflow_payload(workflow)
    restored = load_workflow_payload(
        payload,
        storage_path=tmp_path / "assigned",
    )

    assert payload["kind"] == "archive_v1"
    assert len(payload["payload"]["custom_sources"]) == 1
    assert restored.storage_path == (tmp_path / "assigned").absolute()
    assert restored.to_dict(include_custom_tools=True) == payload["payload"]
