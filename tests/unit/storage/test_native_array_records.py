"""Native NPY records retain array semantics and exact immutable admission."""

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from bioimageflow.cache import dataframe_publish
from bioimageflow.engine.common import _to_python
from bioimageflow.engine.shared_arrays import publish_inputs
from bioimageflow.storage import CacheCorruptionError, Storage, canonical_dataframe_digest
from bioimageflow import DataFrameTool, Workflow
from bioimageflow_core import GENERAL_ENV, IOModel, ProcessingTool, RowConsumption


class CallbackRows(DataFrameTool):
    environment = GENERAL_ENV
    accepts_upstream = False
    executions = 0

    class Inputs(IOModel):
        pass

    class Outputs(IOModel):
        ordinal: int

    def transform(self, dataframe, arguments):
        type(self).executions += 1
        return pd.DataFrame({"ordinal": [0, 1, 2]})


class RetainedRowArrays(ProcessingTool):
    environment = GENERAL_ENV
    row_consumption = RowConsumption.MAPPED
    producers = []

    class Inputs(IOModel):
        ordinal: int

    class Outputs(IOModel):
        pixels: np.ndarray

    def process_row(self, arguments):
        if arguments.ordinal == 2:
            for producer in type(self).producers:
                producer[:] = 99
        pixels = np.array([4, 7, 9][arguments.ordinal], dtype=np.uint16).reshape(1)
        type(self).producers.append(pixels)
        return self.Outputs(pixels=pixels)


def test_direct_accepts_native_outputs_before_next_callback(tmp_path):
    RetainedRowArrays.producers = []
    with Workflow(engine="direct", storage_path=tmp_path / "results") as workflow:
        rows = CallbackRows()(name="rows")
        output = RetainedRowArrays()(ordinal=rows["ordinal"], name="native")
    try:
        result = workflow.compute(output)
        assert [pixels.tolist() for pixels in result["pixels"]] == [[4], [7], [9]]
        assert [pixels.tolist() for pixels in RetainedRowArrays.producers] == [[99], [99], [9]]
        cached = workflow.compute(output)
        assert [pixels.tolist() for pixels in cached["pixels"]] == [[4], [7], [9]]
        assert len(RetainedRowArrays.producers) == 3
    finally:
        workflow.shared_memory_context.close()


def _frame(value):
    return pd.DataFrame({"pixels": pd.Series([value], index=["row"], dtype=object)})


@pytest.mark.parametrize("aligned", [False, True], ids=["packed", "aligned"])
def test_native_record_restores_structured_dtype_and_independent_metadata(tmp_path: Path, aligned):
    producer = np.array([(4, 5), (6, 7)], dtype=np.dtype([("left", ">u2"), ("right", "<i4")], align=aligned))
    selected = dataframe_publish(tmp_path, "native", "signature", _frame(producer),
        run_id="run_" + "0" * 32, engine="direct", tool_identity="test:native")
    accepted = selected.dataframe.at["row", "pixels"]
    assert type(accepted) is np.ndarray and accepted.dtype == producer.dtype
    assert accepted.dtype.isalignedstruct == aligned
    if aligned:
        unaligned = producer.view(np.lib.format.descr_to_dtype(np.lib.format.dtype_to_descr(producer.dtype)))
        assert canonical_dataframe_digest(_frame(producer)) != canonical_dataframe_digest(_frame(unaligned))
    np.testing.assert_array_equal(accepted, producer)
    producer["left"] = 99
    assert accepted["left"].tolist() == [4, 6]
    borrowed = _to_python(accepted)
    assert np.shares_memory(borrowed, accepted)
    borrowed.shape = (2, 1)
    assert accepted.shape == (2,)
    reread = Storage(tmp_path).load_record_dataframe(selected.result_key, selected.record_id,
                                                  hydrate_assets=True).at["row", "pixels"]
    assert reread.shape == (2,) and reread.dtype == accepted.dtype
    assert reread.dtype.isalignedstruct == aligned
    assert not reread.flags.writeable
    with pytest.raises(ValueError):
        reread.setflags(write=True)
    output = next(item for item in selected.manifest.outputs if item.get("asset_role") == "native_array")
    (selected.record_dir / output["path"]).write_bytes(b"corrupt")
    with pytest.raises(CacheCorruptionError, match="size mismatch|digest mismatch"):
        Storage(tmp_path).load_record(selected.result_key, selected.record_id, hydrate_assets=True)


def test_native_input_capture_isolates_current_pixels_and_refreshes_next_capture():
    producer = np.array([4, 5], dtype=np.uint16)
    captured = publish_inputs({"pixels": producer})
    key = canonical_dataframe_digest(_frame(captured["pixels"]))
    producer[:] = 99
    assert captured["pixels"].tolist() == [4, 5]
    assert canonical_dataframe_digest(_frame(captured["pixels"])) == key
    next_capture = publish_inputs({"pixels": producer})
    assert next_capture["pixels"].tolist() == [99, 99]
    assert canonical_dataframe_digest(_frame(next_capture["pixels"])) != key
    same_width_other_fields = np.array([(4, 5)], dtype=[("different", "u2"), ("other", "u2")])
    assert canonical_dataframe_digest(_frame(same_width_other_fields)) != canonical_dataframe_digest(
        _frame(np.array([(4, 5)], dtype=[("left", "u2"), ("right", "u2")])) )


def test_working_frame_owns_native_pixels_containers_and_index_metadata():
    from bioimageflow.result_groups import working_dataframe
    from bioimageflow_core import accept_native_array
    pixels = accept_native_array(np.array([4, 5], dtype=np.uint16))
    nested = {"labels": [4]}
    original = pd.DataFrame({"pixels": pd.Series([pixels], dtype=object),
                             "nested": pd.Series([nested], dtype=object),
                             "nullable": pd.Series([4], dtype="UInt64")})
    original.index = pd.Index(["row"])
    work = working_dataframe(original)
    work.at["row", "pixels"][:] = 99
    work.at["row", "nested"]["labels"][0] = 99
    work.index.values[0] = "changed"
    assert pixels.tolist() == [4, 5] and nested == {"labels": [4]}
    assert original.index.tolist() == ["row"]
    assert work["nullable"].dtype == original["nullable"].dtype
    assert work.iat[0, 0].flags.writeable


def test_callback_publication_keeps_grant_pending_until_failed_task_drains(tmp_path):
    from bioimageflow.engine.shared_arrays import SharedTaskScope
    from bioimageflow_core import SharedArray, SharedMemoryContext
    from bioimageflow_core.shm import create_shared_output, open_shared_array

    class Outputs(IOModel):
        pixels: SharedArray

    owner = SharedMemoryContext(tmp_path)
    scope = SharedTaskScope(owner, "callback-failure", [])
    try:
        with scope.borrowed().activate(), create_shared_output(np.array([4], dtype="uint16")) as producer:
            pass
        accepted = scope.publish_outputs([[Outputs(pixels=producer)]])[0][0].pixels
        assert accepted.bound_group is None
        assert owner.status().pending_grants == 1
        with open_shared_array(producer, writable=True) as pixels:
            pixels[:] = 99
        del pixels
        with open_shared_array(accepted) as pixels:
            assert pixels.tolist() == [4]
        del pixels
        # A later callback failure rejects the task, but cannot retire a live grant.
        scope.fail()
        assert owner.status().pending_grants == 1
        assert scope.context.status().state == "pending"
        scope.drained()
        assert scope.context.status().state == "closed"
        assert owner.status().pending_grants == 0
    finally:
        scope.drained()
        owner.close()


@pytest.mark.parametrize("tool_class", [RetainedRowArrays, CallbackRows])
def test_execution_epoch_prevents_reuse_of_pre_acceptance_contract(tmp_path, monkeypatch, tool_class):
    from dataclasses import replace
    from bioimageflow import WorkflowExecutionContext
    from bioimageflow import worker_origins

    RetainedRowArrays.producers = []
    CallbackRows.executions = 0
    original_capture = worker_origins.capture_tool_executable

    def prior_capture(*args, **kwargs):
        captured = original_capture(*args, **kwargs)
        return replace(captured, scientific_key={name: value for name, value in captured.scientific_key.items()
                                               if name != "execution_contract"})

    contexts = [WorkflowExecutionContext() for _ in range(3)]
    with Workflow(engine="direct", storage_path=tmp_path / "records") as workflow:
        node = tool_class()(name="epoch", **({"ordinal": 0} if tool_class is RetainedRowArrays else {}))
    try:
        # Produce an actual record with the prior scientific-key projection.
        monkeypatch.setattr(worker_origins, "capture_tool_executable", prior_capture)
        workflow.compute(node, run_context=contexts[0])
        monkeypatch.setattr(worker_origins, "capture_tool_executable", original_capture)
        workflow.compute(node, run_context=contexts[1])
        workflow.compute(node, run_context=contexts[2])
        outcomes = [next(item for item in context.execution_outcomes if item.node_key == "epoch")
                    for context in contexts]
        assert outcomes[0].result_key != outcomes[1].result_key
        assert (outcomes[1].result_key, outcomes[1].record_id) == (outcomes[2].result_key, outcomes[2].record_id)
        assert (len(RetainedRowArrays.producers) if tool_class is RetainedRowArrays else CallbackRows.executions) == 2
    finally:
        workflow.shared_memory_context.close()
