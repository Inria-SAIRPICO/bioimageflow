"""Public controller ownership, independent of operation result notification."""
from __future__ import annotations

import gc
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from bioimageflow import Workflow
from bioimageflow.env_manager import WetlandsEnvManager
from bioimageflow.engine.shared_arrays import SharedTaskScope
from bioimageflow.cache.processing_lookup import _rehydrate_processing_assets
from bioimageflow_core import IOModel, SharedArray, SharedMemoryContext
from bioimageflow_core.shm import create_shared_output, open_shared_array
from tests.testkit.runtime_cache import SourceSharedMemoryWriter


def test_temporary_workflow_result_retains_owner_and_live_derived_view(tmp_path):
    with Workflow(engine="direct", storage_path=tmp_path) as workflow:
        node = SourceSharedMemoryWriter()()
    frame = workflow.compute(node)
    ref = frame.at["0", "result"]
    del workflow, frame
    gc.collect()
    with open_shared_array(ref) as array:
        view = np.asarray(array)[1:]
    del array
    assert ref.bound_group.release().state == "pending"
    assert ref.bound_owner.close().state == "pending"
    assert view.tolist() == [[0, 0]]
    with pytest.raises(RuntimeError, match="closing|released"):
        with open_shared_array(ref):
            pass
    del view
    gc.collect()
    assert ref.bound_owner.status().state == "closed"


def test_result_notification_does_not_release_grant_and_failed_pool_close_retries(tmp_path):
    owner = SharedMemoryContext(tmp_path)
    scope = SharedTaskScope(owner, "task", [])
    borrowed = scope.borrowed()
    with borrowed.activate(), create_shared_output(np.arange(3)) as ref:
        pass
    class Outputs(IOModel):
        value: SharedArray
    output = scope.accept_outputs([[Outputs(value=ref)]])[0][0].value
    output.bound_group.release()
    owner.close()
    assert owner.status().pending_grants == 1
    # A live grant retains backing although controller opens are now refused.
    with borrowed.activate(), open_shared_array(ref) as array:
        assert array.tolist() == [0, 1, 2]
    del array
    class Pool:
        fail = True
        def close(self):
            if self.fail:
                raise RuntimeError("not drained")
    manager = WetlandsEnvManager.__new__(WetlandsEnvManager)
    import threading
    manager._lock = threading.RLock()
    pool = Pool()
    manager._pools = {"test": pool}
    manager._pool_configs = {"test": (1, None)}
    manager._environments = {"test": object()}
    manager._specs = {"test": object()}
    manager._shared_memory_grants = {"test": [scope]}
    manager._processing_tasks = {}
    assert manager.stop("test") is False
    assert manager.is_running("test")
    assert owner.status().pending_grants == 1
    pool.fail = False
    assert manager.stop("test") is True
    assert not manager.is_running("test")
    assert output.bound_owner.status().state == "closed"
    assert owner.status().state == "closed"


def test_cache_hydration_later_failure_retires_only_new_group(tmp_path):
    from bioimageflow.storage import CacheCorruptionError
    owner = SharedMemoryContext(tmp_path / "owned")
    with owner.activate(), create_shared_output(np.array([7])) as prior:
        pass
    assets = tmp_path / "record" / "assets" / "shm"
    assets.mkdir(parents=True)
    np.save(assets / "first.npy", np.array([1], dtype="uint16"))
    outputs = [{"kind": "owned_asset", "asset_role": "shared_array",
                "path": "assets/shm/first.npy",
                "array": {"shape": [1], "dtype": "uint16", "order": "C"}}]
    frame = pd.DataFrame({"image": ["assets/shm/first.npy", "assets/shm/missing.npy"]})
    with owner.activate(), pytest.raises(CacheCorruptionError, match="missing manifest"):
        _rehydrate_processing_assets(frame, tmp_path / "record", set(), {"image"}, outputs)
    assert list((tmp_path / "owned").rglob("*.npy")) == [
        next((tmp_path / "owned").rglob(prior.name + ".npy"))]
    with open_shared_array(prior) as array:
        assert array.tolist() == [7]
    assert (assets / "first.npy").exists()
    del array
    owner.close()


def test_unadmitted_result_does_not_take_sibling_or_input_ownership(tmp_path):
    owner = SharedMemoryContext(tmp_path)
    with owner.activate(), create_shared_output(np.array([5])) as input_ref:
        pass
    with owner.activate(), create_shared_output(np.array([9])) as sibling_ref:
        pass
    scope = SharedTaskScope(owner, "task", [input_ref])
    class Outputs(IOModel):
        value: SharedArray
    try:
        with pytest.raises(ValueError, match="unadmitted"):
            scope.accept_outputs([[Outputs(value=sibling_ref)]])
        scope.fail()
    finally:
        scope.drained()
    for ref, expected in ((input_ref, 5), (sibling_ref, 9)):
        with open_shared_array(ref) as array:
            assert array.tolist() == [expected]
        del array
    owner.close()


def test_accepted_cache_identity_uses_content_without_pixel_reopen(tmp_path, monkeypatch):
    from bioimageflow.cache import deterministic_serialize
    owner = SharedMemoryContext(tmp_path)
    first = owner.publish(owner.create(np.array([4, 7], dtype="uint16")))
    same = owner.publish(owner.create(np.array([4, 7], dtype="uint16")))
    changed = owner.publish(owner.create(np.array([4, 99], dtype="uint16")))
    def no_pixels(*args, **kwargs):
        raise AssertionError("Accepted cache identity reread scientific pixels")
    monkeypatch.setattr("bioimageflow_core._shared_storage.map_array", no_pixels)
    try:
        assert first.name != same.name
        assert deterministic_serialize(first) == deterministic_serialize(same)
        assert deterministic_serialize(changed) != deterministic_serialize(first)
        assert "_owner" not in deterministic_serialize(first)
        with pytest.raises(ValueError, match="owner"):
            deterministic_serialize(SharedArray(first.name, first.shape, first.dtype, first.scope_id))
    finally:
        owner.close()


def test_pool_drain_before_controller_interpretation_retains_unpublished_output(tmp_path):
    import threading
    owner = SharedMemoryContext(tmp_path)
    class Outputs(IOModel):
        value: SharedArray
    try:
        # Both dispositions start pending while the worker has finished.
        for decision in ("accept", "reject"):
            scope = SharedTaskScope(owner, decision, [])
            borrowed = scope.borrowed()
            with borrowed.activate(), create_shared_output(np.array([13])) as ref:
                pass
            class Pool:
                closed = False
                def close(self):
                    self.closed = True
            pool = Pool()
            manager = WetlandsEnvManager.__new__(WetlandsEnvManager)
            manager._lock = threading.RLock()
            manager._pools = {decision: pool}
            manager._pool_configs = {decision: (1, None)}
            manager._environments = {decision: object()}
            manager._specs = {decision: object()}
            manager._shared_memory_grants = {decision: [scope]}
            manager._processing_tasks = {}
            assert manager.stop(decision) is True and pool.closed
            assert (Path(scope.context.descriptor()["root"]) / (ref.name + ".npy")).exists()
            if decision == "accept":
                accepted = scope.accept_outputs([[Outputs(value=ref)]])[0][0].value
                with open_shared_array(accepted) as array:
                    assert array.tolist() == [13]
                del array
                accepted.bound_group.release()
            else:
                scope.fail()
            assert scope.context.status().state == "closed"
    finally:
        owner.close()


def test_admitted_controller_task_keeps_independent_input_after_caller_release(tmp_path):
    from bioimageflow.result_groups import bind_result_group
    owner = SharedMemoryContext(tmp_path)
    producer = owner.create(np.array([4], dtype="uint16"))
    accepted = owner.publish(producer)
    caller, group_a = bind_result_group(accepted, node_name="caller", group_id="caller-a")
    scope = SharedTaskScope(owner, "admitted", {"input": caller})
    assert group_a is not None
    group_a.release()
    with pytest.raises(RuntimeError, match="released"):
        with open_shared_array(caller):
            pass
    borrowed = scope.borrowed()
    class Outputs(IOModel):
        value: SharedArray
    try:
        admitted = borrowed.bind_value(scope.inputs)["input"]
        assert scope.inputs["input"].bound_owner is accepted.bound_owner
        assert admitted == accepted
        with open_shared_array(admitted) as array:
            assert array.tolist() == [4] and not array.flags.writeable
        del array
        output = scope.accept_outputs([[Outputs(value=admitted)]])[0][0].value
        assert output.bound_owner is accepted.bound_owner
        assert output == accepted
        scope.drained()
        with open_shared_array(output) as array:
            view = np.asarray(array)
        del array
        assert output.bound_group.release().state == "pending"
        assert view.tolist() == [4]
        del view
        assert output.bound_group.status().state == "closed"
    finally:
        scope.drained()
        owner.close()


def test_failed_input_publication_rolls_back_only_new_snapshots(tmp_path):
    from bioimageflow.engine.shared_arrays import publish_inputs
    owner = SharedMemoryContext(tmp_path)
    original = owner.create(np.array([4], dtype="uint16"))
    foreign = SharedArray("unbound", (1,), "uint16", "foreign")
    try:
        before = owner.status().pending_files
        with pytest.raises(ValueError, match="owner"):
            publish_inputs([original, foreign])
        assert owner.status().pending_files == before
        with open_shared_array(original) as pixels:
            assert pixels.tolist() == [4] and pixels.flags.writeable
        del pixels
    finally:
        owner.close()
