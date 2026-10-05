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


def test_nested_owner_activation_restores_exact_caller_without_closing(tmp_path):
    from bioimageflow_core import SharedMemoryContext, get_shared_memory_context
    from bioimageflow_core.shm import create_shared_output

    caller = SharedMemoryContext(tmp_path / "caller")
    owner = SharedMemoryContext(tmp_path / "owner")
    with caller.activate():
        with owner:
            with owner:
                assert get_shared_memory_context() is owner
            assert get_shared_memory_context() is owner
            with create_shared_output(np.array([9])) as ref:
                assert ref.bound_owner is owner
            with owner.activate():
                with owner.activate():
                    assert get_shared_memory_context() is owner
            assert get_shared_memory_context() is owner
        assert get_shared_memory_context() is caller
    array = owner.open(ref)
    np.testing.assert_array_equal(array, [9])
    del array
    assert owner.close().state == caller.close().state == "closed"


@pytest.mark.parametrize("task", [False, True])
def test_failed_marker_acquisition_retires_only_new_namespace(tmp_path, monkeypatch, task):
    from pathlib import Path
    from bioimageflow_core import SharedMemoryContext

    owner = SharedMemoryContext(tmp_path) if task else None
    sibling = owner.task_scope("sibling") if owner else None
    original_ref = owner.publish(owner.create(np.array([4]))) if owner else None
    sibling_ref = sibling.publish(sibling.create(np.array([7]))) if sibling else None
    parent = Path(owner.descriptor()["root"]) if owner else tmp_path
    foreign = parent / "foreign"
    foreign.mkdir()
    sentinel = foreign / "sentinel"
    sentinel.write_text("keep")
    before = set(parent.iterdir())
    original_write = Path.write_text

    def denied(path, *args, **kwargs):
        if path.name == ".scope.json" and path.parent not in before:
            raise PermissionError("marker write denied")
        return original_write(path, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", denied)
    with pytest.raises(PermissionError, match="marker write denied"):
        owner.task_scope("failed") if owner else SharedMemoryContext(tmp_path)
    assert set(parent.iterdir()) == before
    assert sentinel.read_text() == "keep"
    if owner:
        for context, ref, value in [(owner, original_ref, 4), (sibling, sibling_ref, 7)]:
            array = context.open(ref)
            np.testing.assert_array_equal(array, [value])
            del array
        # Outside entries remain caller-owned; the SDK must not remove them.
        sentinel.unlink()
        foreign.rmdir()
        assert owner.close().state == "closed"


def test_failed_acquisition_cleanup_keeps_primary_error_and_exact_pending_root(tmp_path, monkeypatch):
    from pathlib import Path
    from bioimageflow_core import SharedMemoryContext

    monkeypatch.setattr(Path, "write_text", lambda *_args, **_kwargs: (_ for _ in ()).throw(PermissionError("primary marker")))
    original_rmdir = Path.rmdir
    monkeypatch.setattr(Path, "rmdir", lambda *_: (_ for _ in ()).throw(OSError("secondary cleanup")))
    with pytest.raises(PermissionError, match="primary marker") as caught:
        SharedMemoryContext(tmp_path)
    pending = caught.value.shared_scope_cleanup
    assert pending["state"] == "pending" and pending["errors"] == ("secondary cleanup",)
    root = Path(pending["root"])
    assert root.exists() and [root.stat().st_dev, root.stat().st_ino] == pending["root_identity"]
    # Exact witness-owned empty namespace may be retried by its caller.
    monkeypatch.setattr(Path, "rmdir", original_rmdir)
    root.rmdir()


@pytest.mark.parametrize("task", [False, True])
def test_existing_acquisition_target_is_not_adopted_or_removed(tmp_path, monkeypatch, task):
    from pathlib import Path
    from types import SimpleNamespace
    from bioimageflow_core import SharedMemoryContext
    import bioimageflow_core.shared_memory as shared

    owner = SharedMemoryContext(tmp_path) if task else None
    parent = Path(owner.descriptor()["root"]) if owner else tmp_path
    target = parent / (("task_" if task else "bif_shared_") + "a" * 32)
    target.mkdir()
    sentinel = target / "sentinel"
    sentinel.write_text("foreign")
    identity = target.stat().st_ino
    monkeypatch.setattr(shared.uuid, "uuid4", lambda: SimpleNamespace(hex="a" * 32))
    with pytest.raises(FileExistsError):
        owner.task_scope("collision") if owner else SharedMemoryContext(tmp_path)
    assert target.stat().st_ino == identity and sentinel.read_text() == "foreign"
    sentinel.unlink()
    target.rmdir()
    if owner:
        assert owner.close().state == "closed"
