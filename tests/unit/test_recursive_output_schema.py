"""Portable interfaces retain accepted source semantics without execution."""

from copy import deepcopy
from pathlib import Path

import pytest

from bioimageflow import BindingError, Workflow
from bioimageflow.validation import serialize_input_schema
import output_schema_test_tools as declarations


@pytest.fixture(autouse=True)
def no_scientific_callbacks(monkeypatch):
    monkeypatch.setattr(declarations, "callback_calls", 0)
    yield
    assert declarations.callback_calls == 0


def known_child(storage: Path) -> Workflow:
    child = Workflow(name="known_child", storage_path=storage, engine="direct")
    with child:
        source = declarations.KnownImage()(name="image_producer")
        child.output("renamed_image", source["image"], id="published-image")
    return child


def wrap_child(child: Workflow, storage: Path) -> Workflow:
    parent = Workflow(name="parent", storage_path=storage, engine="direct")
    with parent:
        nested = child(name="nested")
        parent.output("outer_image", nested["renamed_image"], id="outer-image")
    return parent


def load_graph(graph, storage: Path) -> Workflow:
    loaded = Workflow.from_dict(
        graph, storage_path=storage, auto_install=False,
        engine="direct", execution="sequential",
    )
    assert loaded.errors == []
    return loaded


def assert_label_refused(workflow: Workflow, output: str, storage: Path):
    with Workflow(storage_path=storage, engine="direct"):
        nested = workflow(name="nested")
        with pytest.raises(BindingError):
            declarations.LabelConsumer()(image=nested[output], name="consumer")
    return nested.get_output_schema()


@pytest.mark.parametrize("optional_schema", ["absent", "null"])
def test_optional_recursive_output_schema_preserves_known_image(tmp_path, optional_schema):
    original = wrap_child(known_child(tmp_path / "child"), tmp_path / "parent")
    explicit = original.to_dict()
    expected = assert_label_refused(original, "outer_image", tmp_path / "imperative")
    restored = load_graph(explicit, tmp_path / "explicit")
    assert assert_label_refused(restored, "outer_image", tmp_path / "explicit-parent") == expected
    graph = deepcopy(explicit)
    child_graph = graph["nodes"][0]["workflow"]
    for definition in (graph, child_graph):
        port = definition["interface"]["outputs"][0]
        assert port["schema"] is not None
        if optional_schema == "absent":
            del port["schema"]
        else:
            port["schema"] = None

    inferred = load_graph(graph, tmp_path / "inferred")

    assert assert_label_refused(inferred, "outer_image", tmp_path / "inferred-parent") == expected
    inferred_graph = inferred.to_dict()
    for actual, declared in (
        (inferred_graph, explicit),
        (inferred_graph["nodes"][0]["workflow"], explicit["nodes"][0]["workflow"]),
    ):
        assert actual["interface"]["outputs"] == declared["interface"]["outputs"]


def test_explicit_different_output_schema_remains_authoritative(tmp_path):
    graph = known_child(tmp_path / "original").to_dict()
    original_schema = graph["interface"]["outputs"][0]["schema"]
    carried_schema = serialize_input_schema(declarations.LabelConsumer)["image"]
    assert carried_schema["image_spec"] != original_schema["image_spec"]
    graph["interface"]["outputs"][0]["schema"] = carried_schema
    loaded = load_graph(graph, tmp_path / "loaded")

    with Workflow(storage_path=tmp_path / "parent", engine="direct"):
        nested = loaded(name="nested")
        consumer = declarations.LabelConsumer()(image=nested["renamed_image"])

    assert consumer is not None
    assert loaded.to_dict()["interface"]["outputs"][0]["schema"] == carried_schema
    assert nested.get_output_schema()["published-image"]["image_spec"] == carried_schema["image_spec"]


def test_unknown_dynamic_output_stays_unknown(tmp_path):
    workflow = Workflow(storage_path=tmp_path / "original", engine="direct")
    with workflow:
        source = declarations.UnknownColumns()(name="dynamic")
        workflow.output("unknown_image", source["runtime_image"], id="unknown-image")
    graph = workflow.to_dict()
    assert graph["interface"]["outputs"][0].get("schema") is None
    graph["interface"]["outputs"][0].pop("schema", None)

    loaded = load_graph(graph, tmp_path / "loaded")

    assert loaded.to_dict()["interface"]["outputs"][0].get("schema") is None
    with Workflow(storage_path=tmp_path / "parent", engine="direct"):
        nested = loaded(name="nested")
        declarations.LabelConsumer()(image=nested["unknown_image"])
    assert nested.get_resolved_output_schema().get("unknown-image") is None


def test_partial_dynamic_source_retains_its_known_image(tmp_path):
    workflow = Workflow(storage_path=tmp_path / "original", engine="direct")
    with workflow:
        unknown = declarations.UnknownColumns()(name="unknown")
        source = declarations.PartialImageColumns()(unknown, name="partial")
        workflow.output("renamed_image", source["image"], id="published-image")
    assert source.get_resolved_output_schema().state == "dynamic"
    assert source.get_resolved_output_schema().get("image") is not None
    graph = workflow.to_dict()
    expected = assert_label_refused(workflow, "renamed_image", tmp_path / "imperative")
    del graph["interface"]["outputs"][0]["schema"]

    loaded = load_graph(graph, tmp_path / "loaded")

    assert assert_label_refused(loaded, "renamed_image", tmp_path / "loaded-parent") == expected
