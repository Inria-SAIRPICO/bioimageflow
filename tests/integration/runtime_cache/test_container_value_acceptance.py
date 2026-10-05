"""Public finite-container acceptance, record fidelity and working input ownership."""

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from bioimageflow import DataFrameTool, Workflow, WorkflowExecutionContext, result_groups
from bioimageflow.storage import Storage
from bioimageflow_core import GENERAL_ENV, IOModel, ProcessingTool, RowConsumption
from bioimageflow_core.shm import create_shared_output, open_shared_array


class ListCellSource(ProcessingTool):
    row_consumption = RowConsumption.MAPPED
    environment = GENERAL_ENV
    executions = 0
    retained = None

    class Inputs(IOModel):
        pass

    class Outputs(IOModel):
        values: list[int]

    def process_row(self, arguments):
        type(self).executions += 1
        values = [4, 2**53 + 1]
        type(self).retained = values
        return self.Outputs(values=values)


class MutateListWorkingFrame(DataFrameTool):
    environment = GENERAL_ENV

    class Inputs(IOModel):
        pass

    class Outputs(IOModel):
        ready: bool

    def merge_dataframes(self, dataframes, arguments):
        return dataframes[0]

    def transform(self, dataframe, arguments):
        dataframe.iloc[0]["values"][0] = 99
        return pd.DataFrame({"ready": [True]}, index=dataframe.index)


class ConsumeListCell(ProcessingTool):
    row_consumption = RowConsumption.MAPPED
    environment = GENERAL_ENV

    class Inputs(IOModel):
        values: list[int]
        ready: bool

    class Outputs(IOModel):
        first: int
        exact_large: int

    def process_row(self, arguments):
        assert arguments.ready
        return self.Outputs(first=arguments.values[0], exact_large=arguments.values[1])


def _outcome(context, name):
    return next(item for item in context.execution_outcomes if item.node_key == name)


def test_list_field_is_one_cell_cold_warm_and_producer_detached(tmp_path):
    ListCellSource.executions = 0
    contexts = [WorkflowExecutionContext(), WorkflowExecutionContext()]
    with Workflow(engine="direct", storage_path=tmp_path / "results") as workflow:
        source = ListCellSource()(name="source")
    try:
        first = workflow.compute(source, run_context=contexts[0])
        assert len(first) == 1
        assert type(first.at["0", "values"]) is list
        assert first.at["0", "values"] == [4, 2**53 + 1]
        ListCellSource.retained[0] = 99
        assert first.at["0", "values"] == [4, 2**53 + 1]
        second = workflow.compute(source, run_context=contexts[1])
        assert type(second.at["0", "values"]) is list
        assert second.at["0", "values"] == [4, 2**53 + 1]
        assert ListCellSource.executions == 1
        a, b = (_outcome(context, "source") for context in contexts)
        assert a.result_key is not None and a.record_id is not None
        assert (a.result_key, a.record_id) == (b.result_key, b.record_id)
        exact = Storage(workflow.storage_path).load_record_dataframe(a.result_key, a.record_id)
        assert type(exact.at["0", "values"]) is list
        assert exact.at["0", "values"] == [4, 2**53 + 1]
    finally:
        workflow.shared_memory_context.close()


def test_list_working_mutation_preserves_pinned_source_and_sibling(tmp_path):
    context = WorkflowExecutionContext()
    with Workflow(engine="direct", storage_path=tmp_path / "results") as workflow:
        source = ListCellSource()(name="source")
        changed = MutateListWorkingFrame()(source, name="working")
        sibling = ConsumeListCell()(values=source["values"], ready=changed["ready"], name="sibling")
    try:
        result = workflow.compute(sibling, run_context=context)
        assert result["first"].tolist() == [4]
        assert result["exact_large"].tolist() == [2**53 + 1]
        selected = _outcome(context, "source")
        assert selected.result_key is not None and selected.record_id is not None
        frame = Storage(workflow.storage_path).load_record_dataframe(selected.result_key, selected.record_id)
        assert type(frame.at["0", "values"]) is list
        assert frame.at["0", "values"] == [4, 2**53 + 1]
    finally:
        workflow.shared_memory_context.close()


class FidelityCellSource(ProcessingTool):
    environment = GENERAL_ENV
    row_consumption = RowConsumption.MAPPED
    executions = 0

    class Inputs(IOModel):
        kind: str

    class Outputs(IOModel):
        payload: dict | tuple

    def process_row(self, arguments):
        type(self).executions += 1
        return self.Outputs(payload=_fidelity_payload(arguments.kind))


def _fidelity_payload(kind):
    if kind == "tuple":
        return ([4, 2**53 + 1], (b"exact", None, True))
    return {
        None: np.uint64(2**63 + 9),
        b"signed": np.int64(-(2**53 + 1)),
        2: (np.complex64(1 + 2j), np.bool_(True)),
        "floats": [float("nan"), float("inf"), float("-inf"), -0.0],
        False: {"bytes": b"\x00\xff", "nested": [(), {}]},
    }


def _assert_fidelity(actual, kind):
    if kind == "tuple":
        assert type(actual) is tuple
        assert type(actual[0]) is list and actual[0] == [4, 2**53 + 1]
        assert type(actual[1]) is tuple and actual[1] == (b"exact", None, True)
        return
    assert type(actual) is dict
    assert [(type(key), key) for key in actual] == [
        (type(None), None), (bytes, b"signed"), (int, 2), (str, "floats"), (bool, False),
    ]
    assert type(actual[None]) is np.uint64 and actual[None] == np.uint64(2**63 + 9)
    assert type(actual[b"signed"]) is np.int64 and actual[b"signed"] == np.int64(-(2**53 + 1))
    assert type(actual[2]) is tuple
    assert type(actual[2][0]) is np.complex64 and actual[2][0] == np.complex64(1 + 2j)
    assert type(actual[2][1]) is np.bool_ and bool(actual[2][1])
    assert type(actual["floats"]) is list
    nan, positive, negative, zero = actual["floats"]
    assert np.isnan(nan) and positive == float("inf") and negative == float("-inf")
    assert type(zero) is float and zero == 0.0 and np.signbit(zero)
    assert actual[False]["bytes"] == b"\x00\xff"
    assert type(actual[False]["nested"]) is list
    assert type(actual[False]["nested"][0]) is tuple
    assert type(actual[False]["nested"][1]) is dict


@pytest.mark.parametrize("kind", ["tuple", "dict"])
def test_container_scalar_fidelity_reuses_exact_record(tmp_path, kind):
    FidelityCellSource.executions = 0
    contexts = [WorkflowExecutionContext(), WorkflowExecutionContext()]
    with Workflow(engine="direct", storage_path=tmp_path / "results") as workflow:
        source = FidelityCellSource()(kind=kind, name="fidelity")
    try:
        first = workflow.compute(source, run_context=contexts[0])
        _assert_fidelity(first.at["0", "payload"], kind)
        second = workflow.compute(source, run_context=contexts[1])
        _assert_fidelity(second.at["0", "payload"], kind)
        assert FidelityCellSource.executions == 1
        a, b = (_outcome(context, "fidelity") for context in contexts)
        assert a.result_key is not None and a.record_id is not None
        assert (a.result_key, a.record_id) == (b.result_key, b.record_id)
        exact = Storage(workflow.storage_path).load_record_dataframe(a.result_key, a.record_id)
        _assert_fidelity(exact.at["0", "payload"], kind)
    finally:
        workflow.shared_memory_context.close()


class NestedAssetSource(ProcessingTool):
    environment = GENERAL_ENV
    row_consumption = RowConsumption.MAPPED
    executions = 0
    producer_native = None
    producer_shared_view = None

    class Inputs(IOModel):
        source_path: Path

    class Outputs(IOModel):
        payload: dict

    def process_row(self, arguments):
        type(self).executions += 1
        native = np.array([4, 5], dtype="uint16")
        with create_shared_output(np.array([4], dtype="uint16")) as shared:
            with open_shared_array(shared) as producer_view:
                type(self).producer_shared_view = producer_view
        type(self).producer_native = native
        return self.Outputs(payload={"source": arguments.source_path, "leaves": ([native, shared],)})


def test_nested_assets_cold_warm_and_exact_group_release(tmp_path):
    source_path = tmp_path / "source.bin"
    source_path.write_bytes(b"source-exact")
    NestedAssetSource.executions = 0
    contexts = [WorkflowExecutionContext(), WorkflowExecutionContext()]
    with Workflow(engine="direct", storage_path=tmp_path / "results") as workflow:
        source = NestedAssetSource()(source_path=source_path, name="assets")
    try:
        first = workflow.compute(source, run_context=contexts[0])
        a = first.at["0", "payload"]
        assert type(a) is dict and type(a["leaves"]) is tuple
        assert type(a["leaves"][0]) is list and a["source"] == source_path
        native_a, shared_a = a["leaves"][0]
        assert isinstance(native_a, np.ndarray) and native_a.dtype == np.dtype("uint16")
        assert native_a.shape == (2,) and native_a.tolist() == [4, 5]
        assert not native_a.flags.writeable
        group_a, = result_groups(first)
        assert shared_a.bound_group is group_a and shared_a.bound_owner is not None
        NestedAssetSource.producer_native[:] = 99
        NestedAssetSource.producer_shared_view[:] = 99
        assert native_a.tolist() == [4, 5]
        with open_shared_array(shared_a) as pixels:
            assert pixels.tolist() == [4] and not pixels.flags.writeable
        del pixels
        second = workflow.compute(source, run_context=contexts[1])
        b = second.at["0", "payload"]
        native_b, shared_b = b["leaves"][0]
        assert b["source"] == source_path and source_path.read_bytes() == b"source-exact"
        assert native_b.dtype == native_a.dtype and native_b.tolist() == [4, 5]
        assert not native_b.flags.writeable
        assert NestedAssetSource.executions == 1
        selected_a, selected_b = (_outcome(context, "assets") for context in contexts)
        assert selected_a.result_key is not None and selected_a.record_id is not None
        assert (selected_a.result_key, selected_a.record_id) == (selected_b.result_key, selected_b.record_id)
        group_b, = result_groups(second)
        assert group_a is not group_b and shared_b.bound_group is group_b
        group_a.release()
        with pytest.raises(RuntimeError, match="releas|clos"):
            with open_shared_array(shared_a):
                pass
        with open_shared_array(shared_b) as pixels:
            assert pixels.tolist() == [4]
        del pixels
        group_b.release()
    finally:
        NestedAssetSource.producer_shared_view = None
        workflow.shared_memory_context.close()


class ExpandListCells(ProcessingTool):
    environment = GENERAL_ENV
    row_consumption = RowConsumption.MAPPED
    executions = 0

    class Inputs(IOModel):
        count: int

    class Outputs(IOModel):
        values: list[int]

    def process_row(self, arguments):
        type(self).executions += 1
        return [self.Outputs(values=[4, position]) for position in range(arguments.count)]


@pytest.mark.parametrize("count", [0, 2], ids=["zero-outputs", "two-output-rows"])
def test_outer_output_list_expands_rows_and_container_field_stays_one_cell(tmp_path, count):
    ExpandListCells.executions = 0
    contexts = [WorkflowExecutionContext(), WorkflowExecutionContext()]
    with Workflow(engine="direct", storage_path=tmp_path / "results") as workflow:
        source = ExpandListCells()(count=count, name="expansion")
    try:
        first = workflow.compute(source, run_context=contexts[0])
        assert len(first) == count
        assert first["values"].tolist() == [[4, position] for position in range(count)]
        assert all(type(value) is list for value in first["values"])
        second = workflow.compute(source, run_context=contexts[1])
        assert second["values"].tolist() == first["values"].tolist()
        assert second.index.tolist() == first.index.tolist()
        assert ExpandListCells.executions == 1
        a, b = (_outcome(context, "expansion") for context in contexts)
        assert a.result_key is not None and a.record_id is not None
        assert (a.result_key, a.record_id) == (b.result_key, b.record_id)
    finally:
        workflow.shared_memory_context.close()
