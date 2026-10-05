"""Public native-array acceptance and execution-owned DataFrame input contracts."""

import numpy as np
import pandas as pd
import pytest

from bioimageflow import DataFrameTool, Workflow, WorkflowExecutionContext
from bioimageflow.storage import Storage
from bioimageflow_core import GENERAL_ENV, IOModel, ProcessingTool, RowConsumption


class NativeArraySource(ProcessingTool):
    environment = GENERAL_ENV
    row_consumption = RowConsumption.MAPPED
    retained = None
    executions = 0

    class Inputs(IOModel):
        length: int

    class Outputs(IOModel):
        pixels: np.ndarray

    def process_row(self, arguments):
        type(self).executions += 1
        pixels = np.arange(arguments.length, dtype=np.uint16) + np.uint16(4)
        type(self).retained = pixels
        return self.Outputs(pixels=pixels)


class ScalarTable(DataFrameTool):
    environment = GENERAL_ENV
    accepts_upstream = False
    retained = None
    executions = 0

    class Inputs(IOModel):
        pass

    class Outputs(IOModel):
        value: int

    def transform(self, dataframe, arguments):
        type(self).executions += 1
        result = pd.DataFrame({"value": np.array([4], dtype="uint64")}, index=["row"])
        type(self).retained = result
        return result


class MutateWorkingTable(DataFrameTool):
    environment = GENERAL_ENV

    class Inputs(IOModel):
        pass

    class Outputs(IOModel):
        ready: bool

    def transform(self, dataframe, arguments):
        dataframe.loc[:, "value"] = 99
        return pd.DataFrame({"ready": [True]}, index=dataframe.index)


class CustomIdentityMerge(MutateWorkingTable):
    def merge_dataframes(self, dataframes, arguments):
        return dataframes[0]


class DoubleSourceValue(ProcessingTool):
    environment = GENERAL_ENV
    row_consumption = RowConsumption.MAPPED
    received = None

    class Inputs(IOModel):
        value: int
        ready: bool

    class Outputs(IOModel):
        double: int

    def process_row(self, arguments):
        assert bool(arguments.ready)
        type(self).received = int(arguments.value)
        return self.Outputs(double=int(arguments.value) * 2)


def _outcome(context, node_name):
    return next(item for item in context.execution_outcomes if item.node_key == node_name)


@pytest.mark.parametrize("length", [1, 2], ids=["singleton", "multi-element"])
def test_native_array_output_retains_shape_dtype_and_detaches_producer(tmp_path, length):
    NativeArraySource.executions = 0
    contexts = [WorkflowExecutionContext(), WorkflowExecutionContext()]
    with Workflow(engine="direct", storage_path=tmp_path / "results") as workflow:
        source = NativeArraySource()(length=length, name="native")
    try:
        result = workflow.compute(source, run_context=contexts[0])
        accepted = result.at["0", "pixels"]
        assert isinstance(accepted, np.ndarray)
        assert accepted.shape == (length,)
        assert accepted.dtype == np.dtype("uint16")
        assert accepted.tolist() == list(range(4, 4 + length))
        NativeArraySource.retained[:] = 99
        assert accepted.tolist() == list(range(4, 4 + length))
        assert not accepted.flags.writeable
        with pytest.raises(ValueError):
            accepted.setflags(write=True)
        cached = workflow.compute(source, run_context=contexts[1]).at["0", "pixels"]
        assert isinstance(cached, np.ndarray)
        assert cached.shape == accepted.shape and cached.dtype == accepted.dtype
        np.testing.assert_array_equal(cached, accepted)
        assert not cached.flags.writeable
        assert NativeArraySource.executions == 1
        a, b = (_outcome(context, "native") for context in contexts)
        assert a.result_key is not None and a.record_id is not None
        assert (a.result_key, a.record_id) == (b.result_key, b.record_id)
    finally:
        workflow.shared_memory_context.close()


@pytest.mark.parametrize("tool", [MutateWorkingTable, CustomIdentityMerge], ids=["default-merge", "custom-identity-merge"])
def test_dataframe_working_mutation_preserves_selected_source_value(tmp_path, tool):
    context = WorkflowExecutionContext()
    with Workflow(engine="direct", storage_path=tmp_path / "results") as workflow:
        source = ScalarTable()(name="source")
        mutated = tool()(source, name="working")
        consumer = DoubleSourceValue()(value=source["value"], ready=mutated["ready"], name="consumer")
    try:
        result = workflow.compute(consumer, run_context=context)
        selected = _outcome(context, "source")
        assert selected.result_key is not None and selected.record_id is not None
        record = Storage(workflow.storage_path).load_record_dataframe(selected.result_key, selected.record_id)
        assert record["value"].tolist() == [4]
        assert record["value"].dtype == np.dtype("uint64")
        assert result["double"].tolist() == [8]
        assert DoubleSourceValue.received == 4
    finally:
        workflow.shared_memory_context.close()


def test_scalar_dataframe_publication_detaches_retained_producer_and_reuses_record(tmp_path):
    ScalarTable.executions = 0
    contexts = [WorkflowExecutionContext(), WorkflowExecutionContext()]
    with Workflow(engine="direct", storage_path=tmp_path / "results") as workflow:
        source = ScalarTable()(name="source")
    try:
        first = workflow.compute(source, run_context=contexts[0])
        assert first["value"].tolist() == [4]
        ScalarTable.retained.loc[:, "value"] = 99
        assert first["value"].tolist() == [4]
        second = workflow.compute(source, run_context=contexts[1])
        assert second["value"].tolist() == [4]
        assert ScalarTable.executions == 1
        a, b = (_outcome(context, "source") for context in contexts)
        assert a.result_key is not None and a.record_id is not None
        assert (a.result_key, a.record_id) == (b.result_key, b.record_id)
        pointer = Storage(workflow.storage_path).load_current(a.result_key)
        assert pointer is not None and pointer.record_id == a.record_id
    finally:
        workflow.shared_memory_context.close()
