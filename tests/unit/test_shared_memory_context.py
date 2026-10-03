"""Allocation ownership and view lifetime in the current shared-array scope."""

import gc
from dataclasses import replace

import numpy as np
import pytest


def test_result_reference_and_derived_views_retain_owner(tmp_path):
    from bioimageflow_core import SharedMemoryContext
    from bioimageflow_core.shm import create_shared_output, open_shared_array

    owner = SharedMemoryContext(tmp_path)
    with owner.activate():
        with create_shared_output(np.arange(8, dtype=np.int32)) as ref:
            pass
    # Ending the creating execution does not even request resource closure.
    with open_shared_array(ref) as array:
        sliced = array[2:6]
        ordinary = np.asarray(sliced)
    del array, sliced
    gc.collect()
    assert owner.close().state == "pending"
    np.testing.assert_array_equal(ordinary, [2, 3, 4, 5])
    with pytest.raises(RuntimeError, match="clos"):
        with open_shared_array(ref):
            pass
    del ordinary
    gc.collect()
    assert owner.status().state == "closed"


def test_child_namespace_remains_admitted_until_physical_drain(tmp_path):
    from bioimageflow_core import SharedMemoryContext
    from bioimageflow_core.shm import create_shared_output, open_shared_array

    owner = SharedMemoryContext(tmp_path)
    task = owner.task_scope("invocation_task")
    grant = task.acquire_worker_grant()
    borrowed = SharedMemoryContext.borrow(task.descriptor())
    assert owner.close().state == "pending"
    with borrowed.activate():
        with create_shared_output(np.arange(3, dtype=np.uint16)) as worker_ref:
            pass
        with open_shared_array(worker_ref) as data:
            np.testing.assert_array_equal(data, [0, 1, 2])
    del data
    gc.collect()
    assert owner.status().state == "pending"
    grant.drained()
    assert owner.status().state == "closed"


def test_allocation_release_is_contained_and_preserves_other_inputs(tmp_path, monkeypatch):
    from bioimageflow_core import SharedMemoryContext
    from bioimageflow_core.shm import create_shared_output, open_shared_array

    owner = SharedMemoryContext(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"unchanged")
    with owner.activate():
        with create_shared_output(np.ones(3, dtype=np.float32)) as first:
            pass
        with create_shared_output(np.zeros(3, dtype=np.float32)) as second:
            pass
    with pytest.raises(ValueError):
        owner.release(replace(first, name="../outside.txt"))
    with pytest.raises(ValueError):
        owner.bind(replace(first, scope_id="unknown"))
    assert owner.release(first).state == "closed"
    with open_shared_array(second) as data:
        assert data.sum() == 0
    del data
    gc.collect()
    # A backing substituted with a symlink is not opened or reclaimed through
    # its target; the captured scope cannot turn a value node into a file path.
    with owner.activate():
        with create_shared_output(np.ones(2, dtype=np.uint8)) as substituted:
            pass
    from pathlib import Path
    backing = Path(owner.descriptor()["root"]) / (substituted.name + ".npy")
    backing.unlink()
    backing.symlink_to(outside)
    with pytest.raises((OSError, ValueError)):
        with open_shared_array(substituted):
            pass
    assert owner.release(substituted).state == "pending"
    assert outside.read_bytes() == b"unchanged"
    backing.unlink()
    owner.release(substituted)
    assert outside.read_bytes() == b"unchanged"
    # Public deletion failure (including Windows mapped-handle refusal) stays
    # pending and is retried; it is never represented as physical reclamation.
    import bioimageflow_core._shared_storage as storage
    original_delete = storage.delete
    monkeypatch.setattr(storage, "delete", lambda *_: (_ for _ in ()).throw(PermissionError("mapped handle")))
    pending = owner.close()
    assert pending.state == "pending" and pending.errors
    monkeypatch.setattr(storage, "delete", original_delete)
    assert owner.close().state == "closed"


def test_budget_and_object_guard_precede_backing_file_creation(tmp_path, monkeypatch):
    from bioimageflow_core import SharedMemoryContext
    from bioimageflow_core.shm import create_shared_output

    owner = SharedMemoryContext(tmp_path, max_bytes=200)
    with owner.activate():
        with pytest.raises(ValueError, match="Python objects"):
            with create_shared_output(np.array([object()], dtype=object)):
                pass
        with pytest.raises(ValueError, match="budget"):
            with create_shared_output(np.zeros(100, dtype=np.float64)):
                pass
        original_write = np.lib.format.write_array

        def failed_write(handle, array, **kwargs):
            original_write(handle, array, **kwargs)
            raise RuntimeError("controlled unpublished write failure")

        monkeypatch.setattr(np.lib.format, "write_array", failed_write)
        with pytest.raises(RuntimeError, match="unpublished"):
            with create_shared_output(np.zeros(1, dtype=np.float32)):
                pass
    assert not list(tmp_path.rglob("*.npy"))
    assert owner.close().state == "closed"
