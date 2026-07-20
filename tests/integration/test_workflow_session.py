"""Incremental editing tests for the recursive workflow wire model."""

from copy import deepcopy

from bioimageflow import Workflow, WorkflowSession


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
        "config": {"storage_path": "./bif_data", "engine": "direct", "execution": "parallel"},
    }


def test_session_round_trips_and_materializes_recursive_graph() -> None:
    source = _graph()
    session = WorkflowSession(source)
    assert session.to_dict() == source
    assert isinstance(session.to_workflow(), Workflow)
    assert session.validate() == []


def test_session_constant_and_enabled_edits_update_cached_workflow() -> None:
    session = WorkflowSession(_graph())
    workflow = session.to_workflow()
    session.set_constant("generate", "values", [3, 4])
    session.set_enabled("generate", False)
    assert workflow.nodes["generate"]._constant_bindings["values"] == [3, 4]
    assert workflow.nodes["generate"].enabled is False


def test_session_structural_edits_invalidate_materialization() -> None:
    session = WorkflowSession(_graph())
    first = session.to_workflow()
    node = deepcopy(_graph()["nodes"][0])
    node["name"] = "other"
    session.add_node(node)
    second = session.to_workflow()
    assert second is not first
    session.remove_node("other")
    assert "other" not in session.nodes
