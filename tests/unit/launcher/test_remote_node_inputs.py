from __future__ import annotations

import json
from pathlib import Path

import pytest

from bioimageflow import (
    LocalUpload,
    RemoteNodePathPlan,
    Workflow,
    inspect_remote_node_paths,
)
from bioimageflow.launcher.node_inputs import normalize_node_input_overrides
from bioimageflow_common_tools import Files


def _workflow(tmp_path: Path) -> Workflow:
    workflow = Workflow(storage_path=tmp_path / "results")
    with workflow:
        Files()(
            path=Path("relative/images"),
            pattern="*.tif",
            name="files",
        )
    return workflow


def test_path_discovery_is_non_reading_serializable_and_files_agnostic(
    tmp_path: Path,
) -> None:
    workflow = _workflow(tmp_path)

    plan = inspect_remote_node_paths(workflow)
    payload = plan.to_dict()

    assert plan.allocates_resources is False
    assert plan.reads_local_files is False
    assert payload["inputs"] == [
        {
            "scoped_node_path": "files",
            "input_name": "files",
            "value_shape": "list",
            "nullable": True,
            "path_picker": None,
            "current_paths": [],
            "cluster_compatible": True,
        },
        {
            "scoped_node_path": "files",
            "input_name": "path",
            "value_shape": "path",
            "nullable": True,
            "path_picker": "folder",
            "current_paths": ["relative/images"],
            "cluster_compatible": False,
        },
    ]
    assert RemoteNodePathPlan.from_dict(json.loads(json.dumps(payload))) == plan
    assert not (tmp_path / "relative/images").exists()

    payload["inputs"][0]["nullable"] = 1
    with pytest.raises(ValueError, match="Invalid RemoteNodePathInput"):
        RemoteNodePathPlan.from_dict(payload)


def test_nested_scoped_node_override_accepts_local_uploads_and_cluster_paths(
    tmp_path: Path,
) -> None:
    child = _workflow(tmp_path)
    parent = Workflow(storage_path=tmp_path / "parent-results")
    with parent:
        child(name="nested")
    local = tmp_path / "images"

    normalized = normalize_node_input_overrides(
        parent,
        {
            "nested/files": {
                "path": LocalUpload(local),
                "files": None,
            }
        },
    )

    assert normalized == (
        ("nested/files", "files", None),
        ("nested/files", "path", LocalUpload(local)),
    )


def test_override_rejects_non_path_unknown_and_connected_inputs(tmp_path: Path) -> None:
    workflow = _workflow(tmp_path)
    with pytest.raises(TypeError, match="not path-shaped"):
        normalize_node_input_overrides(
            workflow,
            {"files": {"pattern": Path("/cluster/pattern")}},
        )
    with pytest.raises(ValueError, match="Unknown input"):
        normalize_node_input_overrides(
            workflow,
            {"files": {"missing": Path("/cluster/data")}},
        )
    connected = Workflow(storage_path=tmp_path / "connected")
    with connected:
        directory = connected.input("directory", Path, id="directory")
        Files()(path=directory, name="files")
    with pytest.raises(ValueError, match="Connected node input"):
        normalize_node_input_overrides(
            connected,
            {"files": {"path": Path("/cluster/data")}},
        )


def test_files_path_lists_are_json_safe_and_lossless(tmp_path: Path) -> None:
    workflow = Workflow(storage_path=tmp_path / "results")
    expected = [Path("images/a.tif"), Path("images/b.tif")]
    with workflow:
        Files()(files=expected, name="files")

    payload = json.loads(json.dumps(workflow.to_dict()))
    loaded = Workflow.from_dict(payload, storage_path=tmp_path / "loaded")

    assert loaded._nodes["files"]._constant_bindings["files"] == expected
