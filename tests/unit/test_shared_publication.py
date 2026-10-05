"""Public accepted backing is independent of producer aliases and SDK writes."""
from pathlib import Path
import gc

import numpy as np
import pytest

from bioimageflow_core import SharedMemoryContext
from bioimageflow_core.io import save_image
from bioimageflow_core.shm import open_shared_array


def _produced(owner):
    task = owner.task_scope("publication")
    borrowed = SharedMemoryContext.borrow(task.descriptor())
    return task, borrowed.create(np.array([4], dtype=np.int64))


def test_acceptance_detaches_retained_producer_write(tmp_path: Path) -> None:
    owner = SharedMemoryContext(tmp_path)
    task, producer = _produced(owner)
    with open_shared_array(producer) as producer_view:
        accepted = task.accept_result({"pixels": producer})["pixels"]
        producer_view[0] = 99
        with open_shared_array(accepted) as accepted_view:
            assert accepted_view.tolist() == [4]
    del producer_view, accepted_view
    gc.collect()
    task.close()
    owner.close()


def test_accepted_default_open_refuses_mutation(tmp_path: Path) -> None:
    owner = SharedMemoryContext(tmp_path)
    task, producer = _produced(owner)
    accepted = task.accept_result(producer)
    with open_shared_array(accepted) as view:
        with pytest.raises(ValueError, match="read-only"):
            view[0] = 99
    del view
    gc.collect()
    task.close()
    owner.close()


@pytest.mark.parametrize("alias_kind", ["hardlink", "symlink"])
def test_sdk_save_refuses_accepted_backing_alias(tmp_path: Path, alias_kind: str) -> None:
    owner = SharedMemoryContext(tmp_path / "owner")
    task, producer = _produced(owner)
    accepted = task.accept_result(producer)
    backing = Path(accepted.bound_owner.descriptor()["root"]) / (accepted.name + ".npy")
    alias = tmp_path / "alias.npy"
    if alias_kind == "hardlink":
        alias.hardlink_to(backing)
    else:
        alias.symlink_to(backing)
    before = backing.read_bytes()
    with pytest.raises(PermissionError):
        save_image(alias, np.array([99], dtype=np.int64), file_writer=np.save)
    assert backing.read_bytes() == before
    alias.unlink()
    task.close()
    owner.close()


def test_publication_deduplicates_and_reuses_only_accepted_backing(tmp_path: Path) -> None:
    owner = SharedMemoryContext(tmp_path)
    mutable = owner.create(np.array([4], dtype=np.int64))
    accepted = owner.publish_value({"first": mutable, "nested": [mutable]})
    first = accepted["first"]
    assert first == accepted["nested"][0]
    assert first.name != mutable.name
    assert owner.publish(first) is first
    assert first.bound_owner is owner
    with open_shared_array(mutable, writable=True) as writer:
        writer[0] = 99
    with open_shared_array(first) as reader:
        assert reader.tolist() == [4]
    with pytest.raises(PermissionError, match="read-only"):
        with open_shared_array(first, writable=True):
            pass
    del writer, reader
    gc.collect()
    assert owner.close().state == "closed"


def test_group_leases_release_independently_and_keep_existing_views(tmp_path: Path) -> None:
    owner = SharedMemoryContext(tmp_path)
    task, producer = _produced(owner)
    accepted = task.accept_result(producer)
    lease_a, lease_b = task.retain(accepted), task.retain(accepted)
    group_a, group_b = object(), object()
    a, b = lease_a.project(group_a), lease_b.project(group_b)
    assert a == b == accepted
    assert a.bound_owner is b.bound_owner is accepted.bound_owner
    assert a.bound_group is group_a and b.bound_group is group_b
    assert lease_a.owns(a) and not lease_a.owns(b)
    with open_shared_array(a) as live_view:
        assert lease_a.release().pending_leases == 1
        with pytest.raises(RuntimeError, match="released"):
            with open_shared_array(a):
                pass
        with open_shared_array(b) as sibling_view:
            assert sibling_view.tolist() == [4]
        lease_b.release()
        assert live_view.tolist() == [4]
        assert lease_b.status().pending_readers >= 1
    del live_view, sibling_view
    gc.collect()
    assert lease_b.status().pending_files == 0
    task.discard_unreturned()
    assert owner.close().state == "closed"


def test_group_release_waits_for_physical_grant_and_settles_namespace(tmp_path: Path) -> None:
    owner = SharedMemoryContext(tmp_path)
    task, producer = _produced(owner)
    accepted = task.accept_result(producer)
    lease = task.retain(accepted)
    ref = lease.project(object())
    grant = task.acquire_worker_grant(ref)
    task.discard_unreturned()
    assert lease.release().pending_grants == 1
    backing_root = Path(task.descriptor()["root"])
    assert backing_root.exists()
    grant.drained()
    assert lease.status().state == "closed"
    assert not backing_root.exists()
    assert owner.close().state == "closed"


def test_content_identity_is_stable_across_copies_and_changes_after_producer_write(tmp_path: Path) -> None:
    owner = SharedMemoryContext(tmp_path)
    producer = owner.create(np.array([4], dtype=np.int64))
    preview = owner.content_identity(producer)
    first, duplicate = owner.publish(producer), owner.publish(producer)
    identity = owner.content_identity(first)
    assert identity == preview == owner.content_identity(duplicate)
    assert first.name != duplicate.name
    with open_shared_array(producer) as writer:
        writer[0] = 99
    newer = owner.publish(producer)
    assert owner.content_identity(first) == identity
    assert owner.content_identity(newer) != identity
    del writer
    gc.collect()
    assert owner.close().state == "closed"


def test_local_group_pins_are_not_processing_wire_data(tmp_path: Path) -> None:
    from bioimageflow_core._processing_values import decode_processing_value, encode_processing_value

    owner = SharedMemoryContext(tmp_path)
    accepted = owner.publish(owner.create(np.array([4], dtype=np.int64)))
    lease = owner.retain(accepted)
    projected = lease.project(object())
    decoded = decode_processing_value(encode_processing_value(projected))
    assert decoded == projected
    assert decoded.bound_owner is None and decoded.bound_group is None
    assert not lease.owns(decoded)
    lease.release()
    assert owner.close().state == "closed"


def test_admitted_task_pass_through_survives_caller_group_release(tmp_path: Path) -> None:
    owner = SharedMemoryContext(tmp_path)
    accepted = owner.publish(owner.create(np.array([4], dtype=np.int64)))
    caller_lease = owner.retain(accepted)
    caller_ref = caller_lease.project(object())
    task = owner.task_scope("already-admitted")
    grant = task.acquire_worker_grant({"pixels": caller_ref})
    caller_lease.release()
    with pytest.raises(RuntimeError, match="released"):
        with open_shared_array(caller_ref):
            pass
    borrowed = SharedMemoryContext.borrow(task.descriptor(), inputs=(owner.descriptor(),))
    from dataclasses import replace
    wire_ref = replace(accepted, _owner=None)
    task_ref = task.accept_result(borrowed.bind(wire_ref))
    output_lease = owner.retain(task_ref)
    output_ref = output_lease.project(object())
    assert output_ref.bound_owner is owner
    assert output_ref.name == accepted.name
    task.discard_unreturned()
    assert output_lease.status().pending_leases == 2
    grant.drained()
    assert output_lease.status().pending_leases == 1
    with open_shared_array(output_ref) as reader:
        assert reader.tolist() == [4]
    del reader
    gc.collect()
    output_lease.release()
    assert owner.close().state == "closed"


def test_uncommitted_retention_rollback_preserves_existing_allocation(tmp_path: Path) -> None:
    owner = SharedMemoryContext(tmp_path)
    accepted = owner.publish(owner.create(np.array([4], dtype=np.int64)))
    lease = owner.retain(accepted)
    projection = lease.project(object())
    lease.cancel_retention()
    lease.cancel_retention()
    with pytest.raises(RuntimeError, match="released"):
        with open_shared_array(projection):
            pass
    with open_shared_array(accepted) as reader:
        assert reader.tolist() == [4]
    del reader
    gc.collect()
    assert owner.close().state == "closed"


def test_malformed_sealed_descriptor_is_refused_before_publication_or_retention(tmp_path: Path) -> None:
    from dataclasses import replace

    owner = SharedMemoryContext(tmp_path)
    accepted = owner.publish(owner.create(np.array([4], dtype=np.int64)))
    malformed = replace(accepted, shape=(2,))
    with pytest.raises(ValueError, match="metadata"):
        owner.publish(malformed)
    with pytest.raises(ValueError, match="metadata"):
        owner.retain(malformed)
    assert owner.status().pending_leases == 0
    assert owner.close().state == "closed"


def test_structured_numeric_seal_obeys_captured_header_budget(tmp_path: Path) -> None:
    owner = SharedMemoryContext(tmp_path, max_header_bytes=10000)
    dtype = np.dtype([("field_" + str(i) + "_" + "é" * 75, "int64") for i in range(100)])
    assert len(str(dtype)) > 4096
    accepted = owner.publish(owner.create(np.zeros(1, dtype=dtype)))
    with open_shared_array(accepted) as pixels:
        assert pixels.dtype == dtype and not pixels.flags.writeable
    del pixels
    gc.collect()
    assert owner.close().state == "closed"
