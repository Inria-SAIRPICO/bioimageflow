"""Whole-table tools accept constants, never row-valued keyword bindings."""

from copy import deepcopy

import pandas as pd
import pytest

from bioimageflow import DataFrameTool, Workflow
from bioimageflow.node import BindingError, SourceToolUpstreamError
from bioimageflow_core import EnvironmentSpec, IOModel, ProcessingTool, RowConsumption


class Numbers(DataFrameTool):
    accepts_upstream = False

    class Inputs(IOModel):
        value: int = 2

    class Outputs(IOModel):
        value: int

    def transform(self, df, arguments):
        return pd.DataFrame({"value": [arguments.value, arguments.value + 1]})


class AddConstant(DataFrameTool):
    class Inputs(IOModel):
        value: int

    class Outputs(IOModel):
        result: int

    def transform(self, df, arguments):
        return pd.DataFrame({"result": df["value"] + arguments.value})


class Double(ProcessingTool):
    row_consumption = RowConsumption.MAPPED
    environment = EnvironmentSpec(name="binding-test", dependencies={"python": ">=3.10"})

    class Inputs(IOModel):
        value: int

    class Outputs(IOModel):
        result: int

    def process_row(self, arguments):
        return self.Outputs(result=arguments.value * 2)


@pytest.mark.parametrize("shorthand", [False, True])
def test_dataframe_keyword_rejects_column_and_node_without_registration(tmp_path, shorthand):
    with Workflow(storage_path=tmp_path, engine="direct", execution="sequential") as workflow:
        source = Numbers()(name="source")
        before = workflow.to_dict()
        with pytest.raises(BindingError, match="requires a constant"):
            AddConstant()(source, value=source if shorthand else source["value"], name="bad")
        assert workflow.to_dict() == before
        result = AddConstant()(source, value=5, name="valid")
    assert workflow.compute(result)["result"].tolist() == [7, 8]


@pytest.mark.parametrize("shorthand", [False, True])
def test_processing_keyword_retains_column_and_node_bindings(tmp_path, shorthand):
    with Workflow(storage_path=tmp_path, engine="direct", execution="sequential") as workflow:
        source = Numbers()(name="source")
        result = Double()(value=source if shorthand else source["value"])
    assert workflow.compute(result)["result"].tolist() == [4, 6]


def test_source_still_rejects_positional_dataframe(tmp_path):
    with Workflow(storage_path=tmp_path, engine="direct", execution="sequential"):
        source = Numbers()()
        with pytest.raises(SourceToolUpstreamError):
            Numbers()(source)


def _child(tmp_path, tool=AddConstant):
    child = Workflow(name="child", storage_path=tmp_path, engine="direct", execution="sequential")
    with child:
        parameter = child.input("value", int, id="input-value")
        if tool is AddConstant:
            source = Numbers()(name="source")
            result = tool()(source, value=parameter, name="consumer")
        else:
            result = tool()(value=parameter, name="consumer")
        child.output("result", result["result"], id="output-result")
    return child


@pytest.mark.parametrize("depth", [1, 2])
def test_recursive_field_rejects_columns_but_retains_constants(tmp_path, depth):
    child = _child(tmp_path)
    if depth == 2:
        wrapper = Workflow(name="wrapper", storage_path=tmp_path, engine="direct", execution="sequential")
        with wrapper:
            parameter = wrapper.input("value", int, id="input-value")
            nested = child(value=parameter, name="nested")
            wrapper.output("result", nested["result"], id="output-result")
        child = wrapper
    with Workflow(storage_path=tmp_path, engine="direct", execution="sequential") as parent:
        source = Numbers()(name="source")
        before = parent.to_dict()
        with pytest.raises(BindingError, match="requires a constant"):
            child(value=source["value"], name="bad")
        assert parent.to_dict() == before
        result = child(value=5, name="valid")
    assert parent.compute(result)["result"].tolist() == [7, 8]


def test_recursive_processing_field_retains_columns(tmp_path):
    child = _child(tmp_path, Double)
    with Workflow(storage_path=tmp_path, engine="direct", execution="sequential") as parent:
        source = Numbers()(name="source")
        result = child(value=source["value"], name="nested")
    assert parent.compute(result)["result"].tolist() == [4, 6]


@pytest.mark.parametrize("nested", [False, True])
@pytest.mark.parametrize("partial", [False, True])
@pytest.mark.parametrize("wrapped", [False, True])
def test_import_rejects_dataframe_column_binding_with_edge_identity(tmp_path, nested, partial, wrapped):
    with Workflow(storage_path=tmp_path, engine="direct", execution="sequential") as workflow:
        source = Numbers()(name="source")
        if not nested:
            AddConstant()(source, value=5, name="consumer")
    if nested:
        child = _child(tmp_path)
        with workflow:
            child(value=5, name="consumer")
    if wrapped:
        with Workflow(storage_path=tmp_path, engine="direct", execution="sequential") as wrapper:
            workflow(name="wrapper")
        document = wrapper.to_dict()
        graph = document["nodes"][0]["workflow"]
    else:
        document = deepcopy(workflow.to_dict())
        graph = document
    consumer = next(node for node in graph["nodes"] if node["name"] == "consumer")
    field = "input-value" if nested else "value"
    consumer["bindings" if nested else "constants"].pop(field)
    graph["edges"].append({
        "id": "invalid-column-edge", "type": "column",
        "source_node": "source", "source_output": "value",
        "target_node": "consumer", "target_input": field,
    })
    _, errors = Workflow.from_dict(document, storage_path=tmp_path, validate_only=True, partial=partial)
    if not partial:
        assert len(errors) == 1
    error = next(error for error in errors if "requires a constant" in error.message)
    assert error.kind == "type_mismatch"
    assert error.node == "consumer"
    assert error.field == field
    assert error.edge == ("source", "consumer", field)
    assert error.edge_id == "invalid-column-edge"
    assert error.path == (("wrapper",) if wrapped else ())
    with pytest.raises((ValueError, BindingError)):
        Workflow.from_dict(document, storage_path=tmp_path)


def test_nested_graph_reports_inner_binding_path(tmp_path):
    child = _child(tmp_path)
    with Workflow(storage_path=tmp_path, engine="direct", execution="sequential") as parent:
        child(value=5, name="nested")
    graph = parent.to_dict()
    inner = graph["nodes"][0]["workflow"]
    # Remove the published field target before replacing it with an invalid edge.
    inner["interface"]["inputs"][0]["targets"] = []
    inner["edges"].append({
        "id": "inner-invalid-edge", "type": "column",
        "source_node": "source", "source_output": "value",
        "target_node": "consumer", "target_input": "value",
    })
    _, errors = Workflow.from_dict(graph, storage_path=tmp_path, validate_only=True, partial=True)
    error = next(error for error in errors if "requires a constant" in error.message)
    assert error.path == ("nested",)
    assert error.node == "consumer"
    assert error.field == "value"
    assert error.edge_id == "inner-invalid-edge"


def test_rejected_rebinding_preserves_published_constants(tmp_path):
    child = _child(tmp_path)
    with Workflow(storage_path=tmp_path, engine="direct", execution="sequential") as parent:
        source = Numbers()(name="source")
        nested = child(value=5, name="nested")
        before = parent.to_dict()
        with pytest.raises(BindingError, match="requires a constant"):
            nested.bind_port("input-value", source["value"])
        assert parent.to_dict() == before
    assert parent.compute(nested)["result"].tolist() == [7, 8]


def test_validation_rejects_structurally_injected_column_binding(tmp_path):
    with Workflow(storage_path=tmp_path, engine="direct", execution="sequential") as workflow:
        source = Numbers()(name="source")
        consumer = AddConstant()(source, value=5, name="consumer")
    consumer._constant_bindings.pop("value")
    consumer._column_bindings["value"] = source["value"]
    consumer._column_binding_edge_ids["value"] = "invalid-edge"
    errors = workflow.validate()
    error = next(error for error in errors if "requires a constant" in error.message)
    assert error.kind == "type_mismatch"
    assert error.node == "consumer"
    assert error.field == "value"
    assert error.edge == ("source", "consumer", "value")
    assert error.edge_id == "invalid-edge"


class TwoConstants(DataFrameTool):
    class Inputs(IOModel):
        left: int
        right: int


@pytest.mark.parametrize("positional", [False, True])
def test_rejected_tool_binding_does_not_publish_symbolic_targets(tmp_path, positional):
    with Workflow(storage_path=tmp_path, engine="direct", execution="sequential") as workflow:
        source = Numbers()(name="source")
        symbolic = workflow.input("table", kind="dataframe") if positional else workflow.input("left", int)
        before = workflow.to_dict()
        args = [symbolic] if positional else []
        with pytest.raises(BindingError, match="requires a constant"):
            TwoConstants()(*args, left=1 if positional else symbolic, right=source["value"], name="invalid")
        assert workflow.to_dict() == before


def test_rejected_workflow_binding_does_not_publish_symbolic_targets(tmp_path):
    child = Workflow(storage_path=tmp_path, engine="direct", execution="sequential")
    with child:
        left = child.input("left", int)
        right = child.input("right", int)
        TwoConstants()(left=left, right=right)
    with Workflow(storage_path=tmp_path, engine="direct", execution="sequential") as parent:
        source = Numbers()(name="source")
        symbolic = parent.input("left", int)
        before = parent.to_dict()
        with pytest.raises(BindingError, match="requires a constant"):
            child(left=symbolic, right=source["value"], name="invalid")
        assert parent.to_dict() == before


def test_rejected_mixed_fanout_rebinding_is_atomic(tmp_path):
    child = Workflow(storage_path=tmp_path, engine="direct", execution="sequential")
    with child:
        value = child.input("value", int, id="input-value")
        Double()(value=value, name="processing")
        source = Numbers()(name="source")
        table = AddConstant()(source, value=value, name="table")
        child.output("result", table["result"])
    with Workflow(storage_path=tmp_path, engine="direct", execution="sequential") as parent:
        source = Numbers()(name="source")
        nested = child(value=5, name="nested")
        before = parent.to_dict()
        with pytest.raises(BindingError, match="requires a constant"):
            nested.bind_port("input-value", source["value"])
        assert parent.to_dict() == before
    assert parent.compute(nested)["result"].tolist() == [7, 8]
