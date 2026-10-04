"""Public graph edits and definition captures have one accepted authority."""
from copy import deepcopy

import pandas as pd
import pytest

from bioimageflow import DataFrameTool, Workflow, WorkflowSession
from bioimageflow.node import BindingError
from bioimageflow_core import EnvironmentSpec, IOModel, ProcessingTool, RowConsumption


class Table(DataFrameTool):
    accepts_upstream = False

    class Inputs(IOModel):
        value: int = 1

    class Outputs(IOModel):
        value: int

    def transform(self, df, arguments):
        return pd.DataFrame({"value": [arguments.value]}, index=["row"])


class Add(DataFrameTool):
    class Inputs(IOModel):
        amount: int

    class Outputs(IOModel):
        value: int

    def transform(self, df, arguments):
        return pd.DataFrame({"value": df["value"] + arguments.amount}, index=df.index)


class Twice(ProcessingTool):
    row_consumption = RowConsumption.MAPPED
    environment = EnvironmentSpec(name="definition-transactions", dependencies={"python": ">=3.10"})

    class Inputs(IOModel):
        value: int

    class Outputs(IOModel):
        value: int

    def process_row(self, arguments):
        return self.Outputs(value=2 * arguments.value)


class DefaultTable(DataFrameTool):
    accepts_upstream = False

    class Inputs(IOModel):
        values: list[int] = [1]

    class Outputs(IOModel):
        value: int

    def transform(self, df, arguments):
        return pd.DataFrame({"value": arguments.values})


def test_late_node_refusal_leaves_graph_and_symbolic_targets_unchanged(tmp_path):
    with Workflow(storage_path=tmp_path, engine="direct") as workflow:
        amount = workflow.input("amount", int, id="amount")
        source = Table()(name="source")
        before = workflow.to_dict()
        with pytest.raises(BindingError, match="Unknown"):
            Add()(source, amount=amount, unexpected=3, name="refused")
        assert workflow.to_dict() == before
        accepted = Add()(source, amount=amount, name="accepted")
        workflow.output("value", accepted["value"])
    assert workflow.compute(inputs={"amount": 4})["value"].tolist() == [5]


def test_wrong_positional_kind_leaves_no_registered_node(tmp_path):
    with Workflow(storage_path=tmp_path, engine="direct") as workflow:
        field = workflow.input("amount", int, id="amount")
        before = workflow.to_dict()
        with pytest.raises(BindingError, match="positional DataFrame"):
            Add()(field, amount=3, name="refused")
        assert workflow.to_dict() == before


def test_refused_exposure_does_not_publish_an_interface(tmp_path):
    with Workflow(storage_path=tmp_path, engine="direct") as workflow:
        source = Table()(name="source")
        consumer = Twice()(value=source["value"], name="consumer")
        before = workflow.to_dict()
        with pytest.raises(ValueError, match="internal data edge"):
            workflow.expose_input(consumer, "value", name="exposed", id="exposed")
        assert workflow.to_dict() == before


def test_missing_later_workflow_input_does_not_publish_parent_target(tmp_path):
    child = Workflow(storage_path=tmp_path, engine="direct")
    with child:
        first = child.input("first", int, id="first")
        child.input("missing", int, id="missing")
        Twice()(value=first, name="twice")
    with Workflow(storage_path=tmp_path, engine="direct") as parent:
        symbolic = parent.input("value", int, id="value")
        before = parent.to_dict()
        with pytest.raises(BindingError, match="Missing required"):
            child(first=symbolic, name="refused")
        assert parent.to_dict() == before


def test_callable_capture_owns_omitted_mutable_default(tmp_path):
    original = deepcopy(DefaultTable.Inputs.values)
    try:
        child = Workflow(storage_path=tmp_path, engine="direct")
        with child:
            node = DefaultTable()(name="source")
            child.output("value", node["value"])
        parent = Workflow(storage_path=tmp_path, engine="direct")
        with parent:
            captured = child(name="captured")
        DefaultTable.Inputs.values[:] = [9]
        assert parent.compute(captured)["value"].tolist() == [1]
        with parent:
            current = child(name="current")
        assert parent.compute(current)["value"].tolist() == [9]
    finally:
        DefaultTable.Inputs.values[:] = original


def test_session_constant_replaces_edge_without_mutating_old_materialization(tmp_path):
    with Workflow(storage_path=tmp_path, engine="direct") as workflow:
        source = Table()(value=3, name="source")
        consumer = Twice()(value=source["value"], name="consumer")
        workflow.output("value", consumer["value"])
    session = WorkflowSession(workflow.to_dict(), storage_path=tmp_path)
    captured = session.to_workflow()
    session.set_constant("consumer", "value", 7)
    assert session.edges == []
    assert session.to_workflow().compute()["value"].tolist() == [14]
    assert WorkflowSession(session.to_dict(), storage_path=tmp_path).to_workflow().compute()["value"].tolist() == [14]
    assert captured.compute()["value"].tolist() == [6]
    before = session.to_dict()
    with pytest.raises((BindingError, ValueError), match="unknown|Unknown|unexpected"):
        session.set_constant("consumer", "unexpected", 4)
    assert session.to_dict() == before


def test_duplicate_loaded_endpoint_refuses_before_any_tool_resolution(tmp_path, monkeypatch):
    with Workflow(storage_path=tmp_path, engine="direct") as workflow:
        first = Table()(name="first")
        Table()(name="second")
        consumer = Twice()(value=first["value"], name="consumer")
        workflow.output("value", consumer["value"])
    graph = workflow.to_dict()
    graph["edges"].append({**graph["edges"][0], "id": "duplicate-endpoint", "source_node": "second"})
    calls = []
    def forbidden_resolution(*args, **kwargs):
        calls.append("resolved")
        raise AssertionError("A malformed later edge must refuse before earlier tool resolution")
    monkeypatch.setattr(Workflow, "_resolve_tool_instance", forbidden_resolution)
    with pytest.raises(ValueError, match="Duplicate.*target endpoint"):
        Workflow.from_dict(graph, storage_path=tmp_path)
    assert calls == []
    assert not list(tmp_path.iterdir())


def test_auto_names_and_reentrant_contexts_belong_to_their_workflow(tmp_path):
    from bioimageflow.node import get_active_workflow
    outer = Workflow(storage_path=tmp_path, engine="direct")
    inner = Workflow(storage_path=tmp_path, engine="direct")
    with outer:
        first = Table()()
        with inner:
            assert get_active_workflow() is inner
        with outer:
            assert get_active_workflow() is outer
        assert get_active_workflow() is outer
        second = Table()()
    assert get_active_workflow() is None
    assert (first.name, second.name) == ("Table_1", "Table_2")
    assert set(outer.nodes) == {"Table_1", "Table_2"}


def test_callable_capture_preserves_bound_reference_without_copying_owner(tmp_path):
    from bioimageflow_core import SharedArray, SharedMemoryContext
    owner = SharedMemoryContext(tmp_path / "arrays", max_bytes=10000)
    try:
        reference = owner.bind(SharedArray("value", (1,), "<i8", owner.scope_id))
        before = owner.status()
        child = Workflow(storage_path=tmp_path / "results", engine="direct")
        with child:
            child.input("array", SharedArray, default=reference, id="array")
        parent = Workflow(storage_path=tmp_path / "results", engine="direct")
        with parent:
            captured = child(name="captured")
        retained = captured.workflow._interface_inputs["array"].default
        assert retained is reference
        assert retained.bound_owner is owner
        assert owner.status() == before
    finally:
        owner.close()


class ConfiguredType(DataFrameTool):
    accepts_upstream = False

    class Inputs(IOModel):
        text: bool = False

    class Outputs(IOModel):
        value: int

    @classmethod
    def resolve_outputs(cls, values):
        return {"value": {"type": "str" if values.get("text") else "int", "image_spec": None}}

    def transform(self, df, arguments):
        return pd.DataFrame({"value": ["text" if arguments.text else 1]})


def test_configured_output_type_is_the_resolved_authority(tmp_path):
    with Workflow(storage_path=tmp_path, engine="direct"):
        node = ConfiguredType()(text=True, name="configured")
    port = node.get_resolved_output_schema().get("value")
    assert port.annotation is str
    assert port.to_wire()["type_spec"] == {"kind": "str"}


@pytest.mark.parametrize("direction", ["input", "output"])
def test_loaded_interface_schema_refuses_before_tool_resolution(tmp_path, monkeypatch, direction):
    with Workflow(storage_path=tmp_path, engine="direct") as workflow:
        source = Table()(name="source")
        workflow.output("value", source["value"])
    graph = workflow.to_dict()
    malformed = {"type": "int", "type_spec": {"kind": "not-a-type"}, "image_spec": None}
    if direction == "input":
        graph["interface"]["inputs"].append({"id": "unused", "name": "unused", "kind": "field", "targets": [], "schema": malformed})
    else:
        graph["interface"]["outputs"][0]["schema"] = malformed
    calls = []
    def forbidden_resolution(*args, **kwargs):
        calls.append("resolved")
        raise AssertionError("Malformed interface schemas must refuse before tool resolution")
    monkeypatch.setattr(Workflow, "_resolve_tool_instance", forbidden_resolution)
    with pytest.raises(ValueError, match="type|schema|descriptor"):
        Workflow.from_dict(graph, storage_path=tmp_path)
    assert calls == []
    assert not list(tmp_path.iterdir())


def test_diagnostic_definition_refuses_before_execution_effects(tmp_path, monkeypatch):
    workflow = Workflow(storage_path=tmp_path, engine="direct")
    with workflow, workflow.capture_errors():
        source = Table()(unexpected=3, name="diagnostic")
        workflow.output("value", source["value"])
    assert workflow.errors
    effects = []
    def forbidden_transform(*args, **kwargs):
        effects.append("executed")
        raise AssertionError("Diagnostic graph must refuse before tool execution")
    monkeypatch.setattr(Table, "transform", forbidden_transform)
    with pytest.raises(BindingError, match="diagnostic.*unresolved construction"):
        workflow.compute()
    assert effects == []
    assert not list(tmp_path.iterdir())
