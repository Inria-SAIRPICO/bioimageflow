"""Explicit runtime storage for callable and serialized nested workflows."""

from pathlib import Path

import pytest

from bioimageflow import Workflow, WorkflowNode
from tests.integration.test_unified_workflows import build_child


def test_callable_nested_workflows_inherit_root_runtime_storage(
    tmp_path: Path,
) -> None:
    leaf_storage = tmp_path / "leaf-results"
    middle_storage = tmp_path / "middle-results"
    root_storage = tmp_path / "root-results"
    leaf = build_child(storage_path=leaf_storage)

    middle = Workflow(name="middle", storage_path=middle_storage, engine="direct")
    with middle:
        nested_leaf = leaf(value=3, name="leaf")
        middle.output("result", nested_leaf["result"], id="middle-output")

    root = Workflow(name="root", storage_path=root_storage, engine="direct")
    with root:
        nested_middle = middle(name="middle")
        root.output("result", nested_middle["result"], id="root-output")

    assert leaf.storage_path == leaf_storage.resolve()
    assert middle.storage_path == middle_storage.resolve()
    assert nested_leaf.workflow.storage_path == middle_storage.resolve()
    assert nested_middle.workflow.storage_path == root_storage.resolve()
    captured_leaf = nested_middle.workflow.nodes["leaf"]
    assert isinstance(captured_leaf, WorkflowNode)
    assert captured_leaf.workflow.storage_path == root_storage.resolve()

    result = root.compute()
    assert result.loc["row", "result"] == 5


@pytest.mark.parametrize("suffix", [".json", ".zip"])
def test_load_uses_explicit_runtime_storage(
    tmp_path: Path,
    suffix: str,
) -> None:
    definition = build_child(storage_path=tmp_path / "build")
    exported = tmp_path / f"workflow{suffix}"
    definition.export(exported)
    runtime_storage = tmp_path / "loaded-results"

    loaded = Workflow.load(exported, storage_path=runtime_storage)

    assert loaded.storage_path == runtime_storage.resolve()
    assert "storage_path" not in loaded.to_dict()["config"]


def test_import_archive_uses_explicit_runtime_storage(tmp_path: Path) -> None:
    definition = build_child(storage_path=tmp_path / "build")
    archive = tmp_path / "workflow.zip"
    definition.export(archive)
    destination = tmp_path / "imported"
    runtime_storage = destination / "results"

    loaded = Workflow.import_archive(
        archive,
        destination,
        storage_path=runtime_storage,
    )

    assert (destination / "workflow.json").exists()
    assert loaded.storage_path == runtime_storage.resolve()
    assert "storage_path" not in loaded.to_dict()["config"]
