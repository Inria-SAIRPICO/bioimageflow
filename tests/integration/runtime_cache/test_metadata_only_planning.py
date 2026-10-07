"""Planning admits exact records without allocating controller array owners."""

from typing import Any

import numpy as np
import pandas as pd
import pytest

from bioimageflow import DataFrameTool, NodePlanStatus, Workflow, WorkflowExecutionContext, result_groups
from bioimageflow.storage import CacheCorruptionError, Storage
from bioimageflow_core import IOModel, SharedArray, SharedMemoryContext
from bioimageflow_core.shm import create_shared_output, open_shared_array


class PlanArrayTable(DataFrameTool):
    executions = 0

    class Inputs(IOModel):
        kind: str

    class Outputs(IOModel):
        payload: Any

    def transform(self, dataframe, arguments):
        type(self).executions += 1
        pixels = np.array([4, 9], dtype="uint16")
        if arguments.kind == "native":
            value = pixels
        else:
            with create_shared_output(pixels) as reference:
                value = (reference,) if arguments.kind == "shared" else {"pixels": (reference,)}
        return pd.DataFrame({"payload": pd.Series([value], index=["row"], dtype=object)})


def _workflow(path, kind):
    with Workflow(engine="direct", storage_path=path) as workflow:
        node = PlanArrayTable()(kind=kind, name="arrays")
    return workflow, node


def _release(workflow, frame):
    for group in result_groups(frame):
        group.release()
    workflow.shared_memory_context.close()


def _refuse_owner(*args, **kwargs):
    raise AssertionError("metadata-only planning allocated a controller array owner")


def test_cold_dataframe_plan_leaves_storage_absent(tmp_path, monkeypatch):
    workflow, _ = _workflow(tmp_path / "absent-records", "native")
    with monkeypatch.context() as patch:
        patch.setattr(SharedMemoryContext, "__init__", _refuse_owner)
        plan = workflow.plan()["arrays"]
    assert plan.status is NodePlanStatus.UNEXECUTED
    assert plan.final_result_key is not None and plan.selected_record_id is None
    assert not workflow.storage_path.exists()


@pytest.mark.parametrize("kind", ["native", "shared", "portable"])
def test_warm_dataframe_plan_preserves_record_admission_and_compute_hydration(tmp_path, monkeypatch, kind):
    PlanArrayTable.executions = 0
    records = tmp_path / "records"
    seed, node = _workflow(records, kind)
    context = WorkflowExecutionContext()
    frame = seed.compute(node, run_context=context)
    [selected] = context.execution_outcomes
    key, record_id = selected.result_key, selected.record_id
    _release(seed, frame)
    del frame
    workflow, node = _workflow(records, kind)
    before = {str(path.relative_to(records)): path.read_bytes() for path in records.rglob("*") if path.is_file()}
    with monkeypatch.context() as patch:
        patch.setattr(SharedMemoryContext, "__init__", _refuse_owner)
        patch.setattr(Storage, "_rehydrate_record_assets", _refuse_owner)
        plan = workflow.plan()["arrays"]
    after = {str(path.relative_to(records)): path.read_bytes() for path in records.rglob("*") if path.is_file()}
    assert before == after
    assert plan.status is NodePlanStatus.CACHED
    assert (plan.final_result_key, plan.selected_record_id) == (key, record_id)
    assert PlanArrayTable.executions == 1

    actual_context = WorkflowExecutionContext()
    actual = workflow.compute(node, run_context=actual_context)
    try:
        value = actual.at["row", "payload"]
        if kind == "shared":
            assert isinstance(value, tuple)
            value = value[0]
        if kind == "portable":
            assert isinstance(value, dict) and isinstance(value["pixels"], tuple)
            value = value["pixels"][0]
        if isinstance(value, SharedArray):
            with open_shared_array(value) as pixels:
                assert pixels.tolist() == [4, 9] and pixels.dtype == np.dtype("uint16")
                assert not pixels.flags.writeable
            del pixels
        else:
            assert isinstance(value, np.ndarray) and value.tolist() == [4, 9]
            assert value.dtype == np.dtype("uint16") and not value.flags.writeable
        [reused] = actual_context.execution_outcomes
        assert (reused.result_key, reused.record_id) == (key, record_id)
        assert PlanArrayTable.executions == 1
    finally:
        _release(workflow, actual)
    del actual, value

    storage = Storage(records)
    manifest, _, address = storage.load_record(key, record_id)
    asset = next(item for item in manifest.outputs if item.get("path", "").endswith(".npy"))
    asset_path = address / asset["path"]
    original = asset_path.read_bytes()
    asset_path.write_bytes(b"corrupt admitted array")
    corrupt, _ = _workflow(records, kind)
    with monkeypatch.context() as patch:
        patch.setattr(SharedMemoryContext, "__init__", _refuse_owner)
        refusal = corrupt.plan()["arrays"]
    assert refusal.status is NodePlanStatus.CORRUPT and refusal.diagnostic
    assert asset_path.read_bytes() != original
    with pytest.raises(CacheCorruptionError):
        storage.load_record(key, record_id)
