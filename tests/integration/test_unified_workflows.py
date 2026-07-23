"""Contract tests for recursive callable Workflow definitions."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import json

import pandas as pd
import pytest

from bioimageflow import DataFrameTool, NodePlanStatus, Workflow, WorkflowNode
from bioimageflow.node import BindingError, IndexAlignmentError
from bioimageflow_core import IOModel


class ValueTable(DataFrameTool):
    accepts_upstream = False
    executions = 0

    class Inputs(IOModel):
        value: int

    class Outputs(IOModel):
        value: int

    def transform(self, df, arguments):
        type(self).executions += 1
        return pd.DataFrame({"value": [arguments.value]}, index=["row"])


class AddValue(DataFrameTool):
    class Inputs(IOModel):
        amount: int

    class Outputs(IOModel):
        result: int

    def transform(self, df, arguments):
        return pd.DataFrame(
            {"result": pd.DataFrame(df)["value"] + arguments.amount},
            index=pd.DataFrame(df).index,
        )


class IdentityTable(DataFrameTool):
    class Inputs(IOModel):
        pass

    class Outputs(IOModel):
        value: int

    def transform(self, df, arguments):
        return pd.DataFrame(df)


class AlternateIndex(DataFrameTool):
    accepts_upstream = False

    class Inputs(IOModel):
        pass

    class Outputs(IOModel):
        other: int

    def transform(self, df, arguments):
        return pd.DataFrame({"other": [9]}, index=["other-row"])


class ExplodeIndex(DataFrameTool):
    class Inputs(IOModel):
        pass

    class Outputs(IOModel):
        child: int

    def transform(self, df, arguments):
        return pd.DataFrame({"child": [10, 20]}, index=["row::0", "row::1"])


class ConsumeResult(DataFrameTool):
    executions = 0

    class Inputs(IOModel):
        pass

    class Outputs(IOModel):
        final: int

    def transform(self, df, arguments):
        type(self).executions += 1
        frame = pd.DataFrame(df)
        return pd.DataFrame({"final": frame["result"] * 2}, index=frame.index)


class FailTable(DataFrameTool):
    accepts_upstream = False

    class Inputs(IOModel):
        pass

    class Outputs(IOModel):
        failure: int

    def transform(self, df, arguments):
        raise ValueError("detached failure")


def build_child(*, storage_path: Path, with_detached: bool = False) -> Workflow:
    workflow = Workflow(
        name="add_value",
        display_name="Add Value",
        storage_path=storage_path,
        engine="direct",
    )
    with workflow:
        value = workflow.input("value", int, id="input-value")
        amount = workflow.input("amount", int, default=2, id="input-amount")
        source = ValueTable()(value=value, name="source")
        added = AddValue()(source, amount=amount, name="add")
        if with_detached:
            ValueTable()(value=99, name="detached")
        workflow.output("result", added["result"], id="output-result")
    return workflow


def test_callable_workflow_snapshots_are_independent(tmp_path: Path) -> None:
    child = build_child(storage_path=tmp_path)
    parent = Workflow(name="parent", storage_path=tmp_path, engine="direct")
    with parent:
        first = child(value=3, name="first")
        second = child(value=10, amount=5, name="second")
        parent.output("first", first["result"], id="output-first")
        parent.output("second", second["result"], id="output-second")

    assert isinstance(first, WorkflowNode)
    assert first["result"].column == "output-result"
    first.workflow.display_name = "Edited Invocation"
    assert child.display_name == "Add Value"
    assert second.workflow.display_name == "Add Value"
    child.display_name = "Edited Factory Result"
    assert first.workflow.display_name == "Edited Invocation"

    result = parent.compute()
    assert result.loc["row"].to_dict() == {"first": 5, "second": 15}


def test_recursive_containment_is_reported_with_full_path(tmp_path: Path) -> None:
    child = build_child(storage_path=tmp_path)
    parent = Workflow(name="parent", storage_path=tmp_path, engine="direct")
    with parent:
        nested = child(value=3, name="nested")
    nested.workflow = parent
    errors = parent.validate()
    assert any(
        error.kind == "construction_failed"
        and error.path == ("nested",)
        and "Recursive workflow containment" in error.message
        for error in errors
    )


def test_partial_recursive_load_reports_nested_error_path(tmp_path: Path) -> None:
    child = build_child(storage_path=tmp_path)
    parent = Workflow(name="parent", storage_path=tmp_path, engine="direct")
    with parent:
        nested = child(value=3, name="nested")
        parent.output("result", nested["result"], id="parent-output")
    graph = parent.to_dict()
    graph["nodes"][0]["workflow"]["nodes"][0]["tool_module"] = "missing.module"
    _, errors = Workflow.from_dict(graph, validate_only=True, partial=True)
    assert any(
        error.kind == "unknown_tool" and error.path == ("nested",)
        for error in errors
    )


def test_recursive_wire_object_containment_is_rejected() -> None:
    graph = Workflow(name="recursive").to_dict()
    graph["nodes"].append({
        "name": "self",
        "type": "workflow",
        "workflow": graph,
        "bindings": {},
    })
    with pytest.raises(ValueError, match="Recursive workflow graph containment"):
        Workflow.from_dict(graph)


def test_root_inputs_share_nested_binding_semantics(tmp_path: Path) -> None:
    child = build_child(storage_path=tmp_path)
    result = child.compute(inputs={"value": 8, "amount": 4})
    assert result.loc["row", "result"] == 12
    with pytest.raises(BindingError, match="Missing required"):
        child.compute()
    with pytest.raises(ValueError, match="Unknown workflow input"):
        child.compute(inputs={"value": 1, "extra": 2})


def test_exposed_input_preserves_local_constant_as_fallback(tmp_path: Path) -> None:
    workflow = Workflow(name="fallback", storage_path=tmp_path, engine="direct")
    with workflow:
        source = ValueTable()(value=5, name="source")
        workflow.expose_input(
            source,
            "value",
            name="value",
            annotation=int,
            id="input-value",
        )
        workflow.output("result", source["value"], id="output-value")
    assert workflow.compute().loc["row", "result"] == 5
    graph = workflow.to_dict()
    assert "value" in graph["nodes"][0]["constants"]
    loaded = Workflow.from_dict(graph)
    assert loaded.compute().loc["row", "result"] == 5
    assert loaded.compute(inputs={"value": 8}).loc["row", "result"] == 8


def test_dataframe_input_and_parent_symbolic_fanout(tmp_path: Path) -> None:
    child = Workflow(name="identity", storage_path=tmp_path, engine="direct")
    with child:
        table = child.input("table", kind="dataframe", id="input-table")
        IdentityTable()(table, name="left")
        right = IdentityTable()(table, name="right")
        child.output("value", right["value"], id="output-value")

    parent = Workflow(name="parent", storage_path=tmp_path, engine="direct")
    with parent:
        value = parent.input("value", int, id="parent-value")
        source = ValueTable()(value=value, name="source")
        nested = child(table=source, name="identity")
        parent.output("result", nested["value"], id="parent-output")

    [target] = parent._interface_inputs["parent-value"].targets
    assert target["node"] == "source"
    assert len(child._interface_inputs["input-table"].targets) == 2
    assert parent.compute(inputs={"value": 7}).loc["row", "result"] == 7
    root_result = child.compute(
        inputs={"table": pd.DataFrame({"value": [4, 5]}, index=["a", "b"])}
    )
    assert root_result["value"].to_dict() == {"a": 4, "b": 5}


def test_recursive_graph_round_trip_is_canonical(tmp_path: Path) -> None:
    child = build_child(storage_path=tmp_path)
    parent = Workflow(name="parent", display_name="Parent", storage_path=tmp_path, engine="direct")
    with parent:
        nested = child(value=11, name="nested")
        parent.output("answer", nested["result"], id="output-answer")

    graph = parent.to_dict()
    assert graph["schema_version"] == 1
    assert graph["nodes"][0]["type"] == "workflow"
    assert Workflow.from_dict(graph).to_dict() == graph
    assert Workflow.from_dict(graph).compute().loc["row", "answer"] == 13

    malformed = dict(graph)
    malformed["unknown"] = True
    with pytest.raises(ValueError, match="fields must be exactly"):
        Workflow.from_dict(malformed)


@pytest.mark.parametrize("mutation", ["node_variant", "node_extra", "edge_variant", "config_extra"])
def test_recursive_parser_rejects_unknown_variants_and_fields(
    tmp_path: Path,
    mutation: str,
) -> None:
    child = build_child(storage_path=tmp_path)
    parent = Workflow(name="parent", storage_path=tmp_path, engine="direct")
    with parent:
        nested = child(value=2, name="nested")
        parent.output("result", nested["result"], id="parent-output")
    graph = deepcopy(parent.to_dict())
    if mutation == "node_variant":
        graph["nodes"][0]["type"] = "unknown"
    elif mutation == "node_extra":
        graph["nodes"][0]["extra"] = True
    elif mutation == "edge_variant":
        graph["edges"].append({
            "type": "unknown",
            "id": "bad-edge",
            "source_node": "nested",
            "target_node": "nested",
        })
    else:
        graph["config"]["extra"] = True
    with pytest.raises(ValueError):
        Workflow.from_dict(graph)


def test_all_internal_terminals_run_but_boundary_hash_uses_published_values(tmp_path: Path) -> None:
    ValueTable.executions = 0
    child = build_child(storage_path=tmp_path, with_detached=True)
    assert child.compute(inputs={"value": 3}).loc["row", "result"] == 5
    assert ValueTable.executions == 2
    plan = child.plan()
    assert set(plan) == {"source", "add", "detached"}


def test_scoped_planning_steps_and_invalidation_do_not_mutate_names(tmp_path: Path) -> None:
    child = build_child(storage_path=tmp_path)
    parent = Workflow(name="parent", storage_path=tmp_path, engine="direct")
    with parent:
        nested = child(value=3, name="nested")
        parent.output("result", nested["result"], id="parent-output")
    structural_names = [node._name for node in nested.internal_nodes]

    parent.compute()
    plan = parent.plan()
    assert {"nested", "nested/source", "nested/add"} <= set(plan)
    assert plan["nested"].final_result_key is None
    assert plan["nested"].status is NodePlanStatus.CACHED
    assert [step.node_name for step in parent.compute_steps(nested)] == [
        "nested/source",
        "nested/add",
    ]
    assert [node._name for node in nested.internal_nodes] == structural_names

    invalidated = parent.invalidate(["nested"], cascade=False)
    assert {selection.node_name for selection in invalidated} == {
        "nested/source",
        "nested/add",
    }


def test_detached_branch_invalidation_does_not_clear_value_consumers(tmp_path: Path) -> None:
    ConsumeResult.executions = 0
    child = build_child(storage_path=tmp_path, with_detached=True)
    parent = Workflow(name="parent", storage_path=tmp_path, engine="direct")
    with parent:
        nested = child(value=3, name="nested")
        consumer = ConsumeResult()(nested, name="consumer")
        parent.output("final", consumer["final"], id="parent-output")
    parent.compute()

    invalidated = parent.invalidate(["nested/detached"])
    assert {selection.node_name for selection in invalidated} == {
        "nested/detached"
    }
    parent.compute()
    assert ConsumeResult.executions == 1
    assert parent.plan()["consumer"].status is NodePlanStatus.CACHED


def test_detached_failure_reports_scoped_path_and_blocks_consumer(tmp_path: Path) -> None:
    ConsumeResult.executions = 0
    child = build_child(storage_path=tmp_path)
    with child:
        FailTable()(name="fail")
    parent = Workflow(name="parent", storage_path=tmp_path, engine="direct")
    with parent:
        nested = child(value=3, name="nested")
        consumer = ConsumeResult()(nested, name="consumer")
    with pytest.raises(ValueError, match="nested/fail"):
        parent.compute(consumer)
    assert ConsumeResult.executions == 0


def test_disabled_published_source_skips_workflow_boundary(tmp_path: Path) -> None:
    child = build_child(storage_path=tmp_path)
    child.disable("add")
    parent = Workflow(name="parent", storage_path=tmp_path, engine="direct")
    with parent:
        nested = child(value=3, name="nested")
        consumer = ConsumeResult()(nested, name="consumer")

    steps = [
        (step.node_name, step.skipped)
        for step in parent.compute_steps(consumer)
    ]

    assert steps == [
        ("nested/source", False),
        ("nested/add", True),
        ("consumer", True),
    ]


def test_zero_output_workflow_executes_and_returns_canonical_empty_frame(tmp_path: Path) -> None:
    ValueTable.executions = 0
    workflow = Workflow(name="side_effects", storage_path=tmp_path, engine="direct")
    with workflow:
        ValueTable()(value=1, name="one")
        ValueTable()(value=2, name="two")
    result = workflow.compute(inputs={})
    assert result.shape == (0, 0)
    assert ValueTable.executions == 2


def test_incompatible_published_output_indexes_are_rejected(tmp_path: Path) -> None:
    workflow = Workflow(name="bad_indexes", storage_path=tmp_path, engine="direct")
    with workflow:
        value = ValueTable()(value=1, name="value")
        other = AlternateIndex()(name="other")
        workflow.output("value", value["value"], id="output-value")
        workflow.output("other", other["other"], id="output-other")
    with pytest.raises(IndexAlignmentError):
        workflow.compute()


def test_published_outputs_align_parent_and_child_indexes(tmp_path: Path) -> None:
    workflow = Workflow(name="aligned_indexes", storage_path=tmp_path, engine="direct")
    with workflow:
        value = ValueTable()(value=1, name="value")
        child = ExplodeIndex()(value, name="child")
        workflow.output("value", value["value"], id="output-value")
        workflow.output("child", child["child"], id="output-child")
    result = workflow.compute()
    assert result.to_dict("records") == [
        {"value": 1, "child": 10},
        {"value": 1, "child": 20},
    ]


def test_from_python_is_fresh_and_calls_exact_factory_once(tmp_path: Path) -> None:
    helper = tmp_path / "helper.py"
    definition = tmp_path / "workflow.py"
    helper.write_text("VALUE = 3\n")
    definition.write_text(
        "from bioimageflow import Workflow\n"
        "from bioimageflow_common_tools import Generate\n"
        "from helper import VALUE\n"
        "calls = 0\n"
        "def build_workflow():\n"
        "    global calls\n"
        "    calls += 1\n"
        "    workflow = Workflow(name=f'loaded_{calls}', engine='direct')\n"
        "    with workflow:\n"
        "        node = Generate()(column_name='value', values=[VALUE], name='value')\n"
        "        workflow.output('value', node['value'], id='output-value')\n"
        "    return workflow\n"
    )

    first = Workflow.from_python(definition)
    helper.write_text("VALUE = 8\n")
    second = Workflow.from_python(definition)
    assert first.name == second.name == "loaded_1"
    assert first.to_dict()["nodes"][0]["constants"]["values"]["value"] == [3]
    assert second.to_dict()["nodes"][0]["constants"]["values"]["value"] == [8]


@pytest.mark.parametrize(
    ("fixture_name", "include_custom_tools"),
    [
        ("unified_workflow_graph.json", False),
        ("unified_workflow_archive.json", True),
    ],
)
def test_golden_recursive_fixtures_round_trip(
    fixture_name: str,
    include_custom_tools: bool,
    tmp_path: Path,
) -> None:
    path = Path("tests/fixtures") / fixture_name
    source = json.loads(path.read_text())
    workflow = Workflow.from_dict(source)
    assert workflow.to_dict(include_custom_tools=include_custom_tools) == source
    workflow.storage_path = tmp_path / "runtime"

    result = workflow.compute()
    if include_custom_tools:
        assert result.iloc[0].to_dict() == {"one": 1, "two": 2}
    else:
        assert result.iloc[0].to_dict() == {"answer": 7}
