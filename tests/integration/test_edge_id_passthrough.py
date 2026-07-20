"""Edge identity tests for the strict recursive workflow graph."""

from pathlib import Path

import pytest

from bioimageflow import Workflow

from .conftest import FileLoader, FilterRows, StubSegmenter


def _column_graph(tmp_path: Path) -> dict:
    with Workflow(engine="direct", storage_path=tmp_path) as workflow:
        source = FileLoader()(path=str(tmp_path), name="load")
        StubSegmenter()(input_image=source["path"], name="seg")
    graph = workflow.to_dict()
    graph["edges"][0]["id"] = "edge-uuid-42"
    return graph


def test_edge_id_round_trips() -> None:
    graph = _column_graph(Path("/tmp/bioimageflow-edge-test"))
    loaded = Workflow.from_dict(graph)
    assert loaded.to_dict()["edges"][0]["id"] == "edge-uuid-42"


def test_edge_id_is_required() -> None:
    graph = _column_graph(Path("/tmp/bioimageflow-edge-test"))
    del graph["edges"][0]["id"]
    with pytest.raises(ValueError, match="Malformed edge"):
        Workflow.from_dict(graph)


def test_dataframe_edges_have_independent_ids(tmp_path: Path) -> None:
    with Workflow(engine="direct", storage_path=tmp_path) as workflow:
        source = FileLoader()(path=str(tmp_path), name="source")
        FilterRows()(source, source, column_name="filename", name="consumer")
    graph = workflow.to_dict()
    graph["edges"][0]["id"] = "first"
    graph["edges"][1]["id"] = "second"

    round_trip = Workflow.from_dict(graph).to_dict()
    assert [edge["id"] for edge in round_trip["edges"]] == ["first", "second"]
    assert [edge["target_position"] for edge in round_trip["edges"]] == [0, 1]


def test_column_validation_error_carries_edge_id(tmp_path: Path) -> None:
    graph = _column_graph(tmp_path)
    graph["edges"][0]["source_output"] = "no_such_column"
    workflow, build_errors = Workflow.from_dict(
        graph,
        validate_only=True,
        partial=True,
    )
    errors = [*build_errors, *workflow.validate()]
    assert any(
        error.kind == "column_not_found" and error.edge_id == "edge-uuid-42"
        for error in errors
    )
