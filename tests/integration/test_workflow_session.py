"""Incremental editing tests for the recursive workflow wire model."""

from copy import deepcopy
from pathlib import Path

from bioimageflow import NodePlanStatus, Workflow, WorkflowSession
from bioimageflow.workflow_node import WorkflowNode
from bioimageflow_common_tools.generate import Generate


def _graph() -> dict:
    return {
        "schema_version": 1,
        "name": "session",
        "display_name": "Session",
        "interface": {"inputs": [], "outputs": []},
        "nodes": [
            {
                "name": "generate",
                "type": "tool",
                "tool_module": "bioimageflow_common_tools.generate",
                "tool_class": "Generate",
                "tool_package": None,
                "tool_package_version": None,
                "constants": {
                    "column_name": {"__type__": "str", "value": "value"},
                    "values": {"__type__": "list", "value": [1]},
                },
            }
        ],
        "edges": [],
        "config": {"engine": "direct", "execution": "parallel"},
    }


def _nested_graph(storage_path: Path) -> dict:
    child = Workflow(name="child", storage_path=storage_path, engine="direct")
    with child:
        generated = Generate()(
            column_name="value",
            values=[1],
            name="generate",
        )
        child.output("value", generated["value"], id="child-output")

    parent = Workflow(name="parent", storage_path=storage_path, engine="direct")
    with parent:
        nested = child(name="nested")
        parent.output("value", nested["value"], id="parent-output")
    return parent.to_dict()


def test_session_round_trips_and_materializes_recursive_graph(tmp_path) -> None:
    source = _graph()
    session = WorkflowSession(source, storage_path=tmp_path)
    assert session.to_dict() == source
    assert "storage_path" not in session.to_dict()["config"]
    assert isinstance(session.to_workflow(), Workflow)
    assert session.to_workflow().storage_path == tmp_path.resolve()
    assert session.validate() == []


def test_session_storage_path_change_updates_loaded_nested_workflow_and_cache(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    storage_a = tmp_path / "storage-a"
    storage_b = tmp_path / "storage-b"
    graph = _nested_graph(storage_a)
    session = WorkflowSession(graph, storage_path=Path("storage-a"))
    workflow = session.to_workflow()
    nested = workflow.nodes["nested"]
    assert isinstance(nested, WorkflowNode)
    assert session.storage_path == storage_a
    assert workflow.storage_path == storage_a
    assert nested.workflow.storage_path == storage_a

    result_a = workflow.compute()
    assert result_a["value"].tolist() == [1]
    assert session.plan()["nested/generate"].status is NodePlanStatus.CACHED
    run_ids_a = {
        path.name
        for path in (storage_a / "views" / "runs").iterdir()
        if path.is_dir()
    }

    session.storage_path = Path("storage-b")

    assert session.storage_path == storage_b
    assert session.to_workflow() is workflow
    assert workflow.storage_path == storage_b
    assert nested.workflow.storage_path == storage_b
    assert session.plan()["nested/generate"].status is NodePlanStatus.UNEXECUTED
    assert not storage_b.exists()

    result_b = workflow.compute()

    assert result_b["value"].tolist() == [1]
    assert session.plan()["nested/generate"].status is NodePlanStatus.CACHED
    assert {
        path.name
        for path in (storage_a / "views" / "runs").iterdir()
        if path.is_dir()
    } == run_ids_a
    assert len(
        [
            path
            for path in (storage_b / "views" / "runs").iterdir()
            if path.is_dir()
        ]
    ) == 1


def test_session_constant_and_enabled_edits_update_cached_workflow(tmp_path) -> None:
    session = WorkflowSession(_graph(), storage_path=tmp_path)
    workflow = session.to_workflow()
    session.set_constant("generate", "values", [3, 4])
    session.set_enabled("generate", False)
    assert workflow.nodes["generate"]._constant_bindings["values"] == [3, 4]
    assert workflow.nodes["generate"].enabled is False


def test_session_structural_edits_invalidate_materialization(tmp_path) -> None:
    session = WorkflowSession(_graph(), storage_path=tmp_path)
    first = session.to_workflow()
    node = deepcopy(_graph()["nodes"][0])
    node["name"] = "other"
    session.add_node(node)
    second = session.to_workflow()
    assert second is not first
    session.remove_node("other")
    assert "other" not in session.nodes
