"""Tests for edge ID passthrough (plan-platform-boundary-refactor.md Task 1).

Edge IDs are an optional opaque identifier that GUIs can attach to edges
via the wire format's ``id`` key. The library:
  - round-trips them through :meth:`Workflow.to_dict` / :meth:`Workflow.from_dict`
  - copies them onto every :class:`ValidationError` raised against the
    matching edge

This is the disambiguator for cases where the structural
``(from_node, to_node, field)`` triple is not unique — most notably
positional args, which all share ``field="__positional__"``.
"""

from pathlib import Path

from bioimageflow import Workflow


def _wf_data(tmp_path: Path) -> dict:
    """A minimal valid workflow with one edge that we can attach an ID to."""
    return {
        "nodes": [
            {
                "name": "load",
                "tool_module": "tests.integration.conftest",
                "tool_class": "FileLoader",
                "constants": {"path": {"__type__": "str", "value": str(tmp_path)}},
                "args": [],
            },
            {
                "name": "seg",
                "tool_module": "tests.integration.conftest",
                "tool_class": "StubSegmenter",
                "constants": {},
                "args": [],
            },
        ],
        "edges": [
            {
                "from": "load", "to": "seg",
                "column": "path", "field": "input_image",
                "id": "edge-uuid-42",
            },
        ],
        "config": {"storage_path": str(tmp_path)},
    }


class TestEdgeIdRoundTrip:
    def test_id_survives_from_dict_to_dict(self, tmp_path: Path) -> None:
        data = _wf_data(tmp_path)
        wf = Workflow.from_dict(data)
        out = wf.to_dict()
        edges_with_id = [e for e in out["edges"] if "id" in e]
        assert len(edges_with_id) == 1
        assert edges_with_id[0]["id"] == "edge-uuid-42"

    def test_edges_without_id_continue_to_work(self, tmp_path: Path) -> None:
        data = _wf_data(tmp_path)
        # Strip the id; should still construct.
        del data["edges"][0]["id"]
        wf = Workflow.from_dict(data)
        out = wf.to_dict()
        assert all("id" not in e for e in out["edges"])

    def test_positional_edges_with_distinct_ids(self, tmp_path: Path) -> None:
        # Two positional edges from same upstream into same downstream;
        # (from, to, field) is identical — only the ID disambiguates.

        data = {
            "nodes": [
                {"name": "src", "tool_module": "tests.integration.conftest",
                 "tool_class": "FileLoader", "constants": {
                     "path": {"__type__": "str", "value": str(tmp_path)},
                 }, "args": []},
                {"name": "dup", "tool_module": "tests.integration.conftest",
                 "tool_class": "FilterRows", "constants": {
                     "column_name": {"__type__": "str", "value": "filename"},
                 }, "args": ["src", "src"]},
            ],
            "edges": [
                {"from": "src", "to": "dup",
                 "column": "__positional__", "field": "__positional__",
                 "id": "first"},
                {"from": "src", "to": "dup",
                 "column": "__positional__", "field": "__positional__",
                 "id": "second"},
            ],
            "config": {"storage_path": str(tmp_path)},
        }
        wf = Workflow.from_dict(data)
        out = wf.to_dict()
        positional_edges = [
            e for e in out["edges"] if e["field"] == "__positional__"
        ]
        ids = [e.get("id") for e in positional_edges]
        # Both IDs survive — distinguishing edges that the (from, to, field)
        # triple cannot.
        assert ids == ["first", "second"]


class TestEdgeIdInValidationError:
    def test_column_not_found_carries_edge_id(self, tmp_path: Path) -> None:
        # Drive a column_not_found error and assert the edge_id is attached.
        data = _wf_data(tmp_path)
        # Reference a column that does not exist in FileLoader's outputs.
        data["edges"][0]["column"] = "no_such_column"
        wf, _ = Workflow.from_dict(
            data, validate_only=True, partial=True,
        )
        errs = wf.validate()
        cnf = [e for e in errs if e.kind == "column_not_found"]
        assert cnf, "expected a column_not_found error"
        assert any(e.edge_id == "edge-uuid-42" for e in cnf)

    def test_validation_error_default_edge_id_is_none(self) -> None:
        from bioimageflow import ValidationError

        err = ValidationError(kind="missing_input", message="x")
        assert err.edge_id is None
