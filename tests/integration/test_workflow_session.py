"""Tests for :class:`WorkflowSession`
(plan-platform-boundary-refactor.md Task 9).
"""

from pathlib import Path

import pytest

from bioimageflow import WorkflowSession, Workflow

from .conftest import FileLoader, StubSegmenter


def _two_node_data(tmp_path: Path) -> dict:
    return {
        "nodes": [
            {
                "name": "load",
                "tool_module": "tests.integration.conftest",
                "tool_class": "FileLoader",
                "constants": {
                    "path": {"__type__": "str", "value": str(tmp_path)},
                },
                "args": [],
            },
            {
                "name": "seg",
                "tool_module": "tests.integration.conftest",
                "tool_class": "StubSegmenter",
                "constants": {
                    "diameter": {"__type__": "float", "value": 30.0},
                },
                "args": [],
            },
        ],
        "edges": [
            {"id": "e1", "from": "load", "to": "seg",
             "column": "path", "field": "input_image"},
        ],
        "config": {"storage_path": str(tmp_path)},
    }


class TestRoundTrip:
    def test_to_dict_round_trip_is_identity(self, tmp_path: Path) -> None:
        data = _two_node_data(tmp_path)
        s = WorkflowSession(data)
        out = s.to_dict()
        # Constants and edges survive the round trip.
        assert out["nodes"][0]["constants"]["path"]["value"] == str(tmp_path)
        assert out["edges"][0]["id"] == "e1"

    def test_to_workflow_returns_a_real_workflow(self, tmp_path: Path) -> None:
        s = WorkflowSession(_two_node_data(tmp_path))
        wf = s.to_workflow()
        assert isinstance(wf, Workflow)
        assert "load" in wf.nodes
        assert "seg" in wf.nodes


class TestEdits:
    def test_add_remove_node(self, tmp_path: Path) -> None:
        s = WorkflowSession({"nodes": [], "edges": [], "config": {}})
        s.add_node({
            "name": "load",
            "tool_module": "tests.integration.conftest",
            "tool_class": "FileLoader",
            "constants": {
                "path": {"__type__": "str", "value": str(tmp_path)},
            },
            "args": [],
        })
        assert "load" in s.nodes
        s.remove_node("load")
        assert "load" not in s.nodes

    def test_add_node_duplicate_raises(self, tmp_path: Path) -> None:
        s = WorkflowSession(_two_node_data(tmp_path))
        with pytest.raises(ValueError):
            s.add_node({"name": "load", "tool_module": "x", "tool_class": "Y",
                        "constants": {}, "args": []})

    def test_remove_edge_strips_orphans(self, tmp_path: Path) -> None:
        s = WorkflowSession(_two_node_data(tmp_path))
        s.remove_edge("e1")
        assert all(e.get("id") != "e1" for e in s.edges)

    def test_set_constant_updates_state(self, tmp_path: Path) -> None:
        s = WorkflowSession(_two_node_data(tmp_path))
        s.set_constant("seg", "diameter", 99.5)
        node = s.nodes["seg"]
        assert node["constants"]["diameter"]["value"] == 99.5

    def test_set_enabled_toggles(self, tmp_path: Path) -> None:
        s = WorkflowSession(_two_node_data(tmp_path))
        s.set_enabled("seg", False)
        assert s.nodes["seg"]["enabled"] is False
        s.set_enabled("seg", True)
        # When re-enabled, the key is dropped (matches to_dict's clean form).
        assert "enabled" not in s.nodes["seg"]


class TestCachingBehavior:
    def test_constant_edit_does_not_re_resolve_tool_class(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Building the workflow once is fine. The contract is that
        # a *subsequent* constant edit + validate() / plan() does NOT
        # re-call resolve_tool_class.
        s = WorkflowSession(_two_node_data(tmp_path))
        s.to_workflow()  # initial build

        from bioimageflow import workflow as workflow_mod

        calls: list[tuple] = []
        original = workflow_mod.Workflow._resolve_tool_instance

        def tracking(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            calls.append((args, kwargs))
            return original(self, *args, **kwargs)

        monkeypatch.setattr(
            workflow_mod.Workflow, "_resolve_tool_instance", tracking,
        )

        s.set_constant("seg", "diameter", 42.0)
        s.validate()
        s.plan()

        assert calls == [], (
            "constant edit should not trigger any tool resolution; "
            f"got {len(calls)} calls"
        )

    def test_structural_edit_invalidates_workflow_cache(
        self, tmp_path: Path,
    ) -> None:
        s = WorkflowSession(_two_node_data(tmp_path))
        wf1 = s.to_workflow()
        s.add_edge({
            "id": "extra",
            "from": "load", "to": "seg",
            "column": "filename", "field": "input_image",
        })
        wf2 = s.to_workflow()
        assert wf1 is not wf2  # rebuild happened

    def test_constant_edit_keeps_workflow_instance(
        self, tmp_path: Path,
    ) -> None:
        s = WorkflowSession(_two_node_data(tmp_path))
        wf1 = s.to_workflow()
        s.set_constant("seg", "diameter", 42.0)
        wf2 = s.to_workflow()
        assert wf1 is wf2  # same instance, mutated in place


class TestValidateAndPlan:
    def test_validate_returns_errors_consistent_with_workflow(
        self, tmp_path: Path,
    ) -> None:
        s = WorkflowSession(_two_node_data(tmp_path))
        errs = s.validate()
        # Reference: build the workflow directly and validate it.
        wf = s.to_workflow()
        ref = wf.validate()
        # Both should report the same kinds (order may differ; compare sets).
        assert {e.kind for e in errs} == {e.kind for e in ref}

    def test_plan_returns_node_plan_entries(self, tmp_path: Path) -> None:
        s = WorkflowSession(_two_node_data(tmp_path))
        plan = s.plan()
        assert "load" in plan
        assert "seg" in plan
