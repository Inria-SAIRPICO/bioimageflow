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
    assert ref.bound_owner.close().state == "pending"
    assert view.tolist() == [[0, 0]]
    with pytest.raises(RuntimeError, match="closing"):
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


def test_cache_identity_excludes_local_owner_binding_without_opening(tmp_path, monkeypatch):
    from dataclasses import replace
    from bioimageflow.cache import deterministic_serialize
    owner = SharedMemoryContext(tmp_path)
    ref = SharedArray("reference", (2,), "uint16", owner.scope_id)
    bound = owner.bind(ref)
    def no_io(*args, **kwargs):
        raise AssertionError("Cache identity opened scientific storage")
    monkeypatch.setattr("bioimageflow_core._shared_storage.map_array", no_io)
    try:
        assert bound == ref
        assert deterministic_serialize(bound) == deterministic_serialize(ref)
        assert "_owner" not in deterministic_serialize(bound)
        assert deterministic_serialize(replace(bound, name="another")) != deterministic_serialize(bound)
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
            assert manager.stop(decision) is True and pool.closed
            assert (Path(scope.context.descriptor()["root"]) / (ref.name + ".npy")).exists()
            if decision == "accept":
                accepted = scope.accept_outputs([[Outputs(value=ref)]])[0][0].value
                with open_shared_array(accepted) as array:
                    assert array.tolist() == [13]
                del array
                scope.context.release(accepted)
            else:
                scope.fail()
            assert scope.context.status().state == "closed"
    finally:
        owner.close()
