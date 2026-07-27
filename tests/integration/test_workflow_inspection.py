"""Tests for the Workflow inspection surface introduced by Task 2 of
plan-platform-boundary-refactor.md: ``failed_nodes``, ``is_partial``,
``errors``.
"""

from pathlib import Path

from bioimageflow import Workflow


def _bad_data(*, valid_count: int = 0) -> dict:
    nodes: list[dict] = [
        {"name": "broken_a", "type": "tool", "tool_module": "no.mod.a", "tool_class": "A",
         "tool_package": None, "tool_package_version": None, "constants": {}},
        {"name": "broken_b", "type": "tool", "tool_module": "no.mod.b", "tool_class": "B",
         "tool_package": None, "tool_package_version": None, "constants": {}},
    ]
    for i in range(valid_count):
        nodes.append({
            "name": f"valid_{i}",
            "type": "tool",
            "tool_module": "tests.testkit.integration_tools",
            "tool_class": "FileLoader",
            "tool_package": None,
            "tool_package_version": None,
            "constants": {"path": {"__type__": "str", "value": "/tmp/x"}},
        })
    return {
        "schema_version": 1,
        "name": "inspection",
        "display_name": "Inspection",
        "interface": {"inputs": [], "outputs": []},
        "nodes": nodes,
        "edges": [],
        "config": {},
    }


class TestFailedNodes:
    def test_failed_nodes_populated_in_partial_mode(self, tmp_path: Path) -> None:
        wf, errs = Workflow.from_dict(
            _bad_data(),
            storage_path=tmp_path,
            validate_only=True,
            partial=True,
        )
        assert set(wf.failed_nodes.keys()) == {"broken_a", "broken_b"}
        for name, err in wf.failed_nodes.items():
            assert err.kind == "unknown_tool"
            assert err.node == name

    def test_failed_nodes_empty_for_clean_build(self, tmp_path: Path) -> None:
        data = {
            "nodes": [{
                "name": "load",
                "type": "tool",
                "tool_module": "tests.testkit.integration_tools",
                "tool_class": "FileLoader",
                "tool_package": None,
                "tool_package_version": None,
                "constants": {"path": {"__type__": "str", "value": "/tmp/x"}},
            }],
            "edges": [],
            "config": {},
            "schema_version": 1,
            "name": "inspection",
            "display_name": "Inspection",
            "interface": {"inputs": [], "outputs": []},
        }
        wf = Workflow.from_dict(data, storage_path=tmp_path)
        assert wf.failed_nodes == {}
        assert wf.errors == []
        assert wf.is_partial is False


class TestIsPartial:
    def test_is_partial_true_when_node_failed(self, tmp_path: Path) -> None:
        wf, _ = Workflow.from_dict(
            _bad_data(valid_count=1),
            storage_path=tmp_path,
            validate_only=True, partial=True,
        )
        assert wf.is_partial is True
        assert "valid_0" in wf.nodes
        assert "broken_a" not in wf.nodes

    def test_is_partial_false_for_programmatic_workflow(self, tmp_path) -> None:
        # A workflow built via the context manager never went through
        # from_dict, so the "expected node names" set is None and
        # is_partial is False.
        wf = Workflow(storage_path=tmp_path, engine="direct")
        assert wf.is_partial is False


class TestErrorsProperty:
    def test_errors_matches_returned_tuple(self, tmp_path: Path) -> None:
        wf, errs = Workflow.from_dict(
            _bad_data(),
            storage_path=tmp_path,
            validate_only=True,
            partial=True,
        )
        assert wf.errors == errs
        # Returned list is a copy: mutating it does not affect the property.
        wf.errors.append(None)  # type: ignore[arg-type]
        assert len(wf.errors) == 2

    def test_errors_empty_for_clean_strict_build(self, tmp_path: Path) -> None:
        data = Workflow(storage_path=tmp_path).to_dict()
        wf = Workflow.from_dict(data, storage_path=tmp_path)
        assert wf.errors == []


class TestNodesViewIsCopy:
    def test_nodes_returns_a_copy(self, tmp_path) -> None:
        wf = Workflow(storage_path=tmp_path, engine="direct")
        wf.nodes["spurious"] = None  # type: ignore[assignment]
        assert "spurious" not in wf._nodes  # internal state untouched
