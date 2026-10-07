"""Owned export hydration has an explicit, retryable public disposal boundary."""

import gc
from pathlib import Path
import tempfile

import pytest

from bioimageflow import Workflow, WorkflowExecutionContext, result_groups
from bioimageflow.launcher.result_download import _load_manifest, _verify_tree
from bioimageflow.launcher.returns import load_public_return_from_bundle
from bioimageflow_core import SharedMemoryContext
from bioimageflow_core.shm import create_shared_output, open_shared_array
from bundle_export_test_tools import ExportArray


@pytest.fixture
def attached_array(tmp_path, monkeypatch):
    hydration = tmp_path / "hydration"
    hydration.mkdir()
    # Select an exclusively owned stdlib temporary directory before hydration.
    # The SDK's actual owner factory and allocation operations remain unchanged.
    monkeypatch.setattr(tempfile, "tempdir", str(hydration))
    owner = SharedMemoryContext(tmp_path / "compute-owner")
    workflow = Workflow(
        name="export-owner-root",
        engine="direct",
        storage_path=tmp_path / "storage",
        shared_memory_context=owner,
    )
    with workflow:
        writer = ExportArray()(name="writer")
    workflow.output("image", writer["image"], id="output-image")
    context = WorkflowExecutionContext(shared_memory_context=owner)
    values = []
    try:
        result = workflow.compute(run_context=context)
        values.append(result)
        yield context, result, values, hydration
    finally:
        for value in values:
            for group in result_groups(value):
                group.release()
            value.at["0", "image"].bound_owner.close()
        owner.close()


def _assert_pixels(reference):
    with open_shared_array(reference) as pixels:
        assert pixels.tolist() == [[11, 11, 11], [11, 11, 11]]
    del pixels


@pytest.mark.shared_memory
def test_materialized_export_group_disposes_its_owned_root_after_view_drain(
    tmp_path, attached_array,
):
    context, result, values, hydration = attached_array
    exported = context.export_result(result, destination=tmp_path / "export")
    values.append(exported)
    reference = exported.at["0", "image"]
    descriptor = reference.bound_owner.descriptor()
    root = Path(descriptor["owner_root"])
    root.relative_to(hydration)
    assert root.is_dir()
    _assert_pixels(reference)
    [group] = result_groups(exported)

    with open_shared_array(reference) as pixels:
        retained_view = pixels[:, :2]
    del pixels
    try:
        pending = group.release()
        assert pending.state == "pending" and pending.pending_readers > 0
        assert retained_view.tolist() == [[11, 11], [11, 11]]
        _assert_pixels(result.at["0", "image"])
    finally:
        del retained_view
        gc.collect()

    closed = group.status()
    assert closed.state == "closed" and not closed.errors
    assert not root.exists(), "Released materialized return left its SDK-owned root"
    _assert_pixels(result.at["0", "image"])


@pytest.mark.shared_memory
def test_materialized_export_root_cleanup_error_is_retained_for_retry(
    tmp_path, attached_array,
):
    context, result, values, hydration = attached_array
    exported = context.export_result(result, destination=tmp_path / "export")
    values.append(exported)
    root = Path(exported.at["0", "image"].bound_owner.descriptor()["owner_root"])
    root.relative_to(hydration)
    obstruction = root / "test-owned-obstruction"
    obstruction.write_text("retain until the exact owner retries")
    [group] = result_groups(exported)
    try:
        pending = group.release()
        assert pending.state == "pending" and pending.errors
        assert root.is_dir() and obstruction.read_text().startswith("retain")
        _assert_pixels(result.at["0", "image"])
    finally:
        obstruction.unlink()

    closed = group.release()
    assert closed.state == "closed" and not closed.errors
    assert not root.exists()


@pytest.mark.shared_memory
def test_owned_return_root_waits_for_a_second_same_root_group(tmp_path, attached_array):
    context, result, values, hydration = attached_array
    destination = tmp_path / "export"
    first = context.export_result(result, destination=destination)
    values.append(first)
    first_reference = first.at["0", "image"]
    root = Path(first_reference.bound_owner.descriptor()["owner_root"])
    root.relative_to(hydration)
    manifest = _load_manifest(destination / "manifest.json")
    second = load_public_return_from_bundle(
        destination,
        manifest["return_manifest"],
        _verify_tree(destination, manifest),
        shared_memory_context=first_reference.bound_owner,
    )
    values.append(second)
    second_reference = second.at["0", "image"]
    assert second_reference.bound_owner.descriptor()["owner_root"] == str(root)
    [first_group] = result_groups(first)
    [second_group] = result_groups(second)
    assert first_group is not second_group
    with open_shared_array(first_reference) as pixels:
        retained_view = pixels[:, :2]
    del pixels
    try:
        first_group.release()
        _assert_pixels(second_reference)
        assert root.is_dir()
        second_group.release()
        assert retained_view.tolist() == [[11, 11], [11, 11]]
    finally:
        del retained_view
        gc.collect()

    assert first_group.status().state == second_group.status().state == "closed"
    assert not root.exists(), "Final same-root group disposal lost root retirement"
    _assert_pixels(result.at["0", "image"])


@pytest.mark.shared_memory
def test_return_loader_does_not_close_a_caller_supplied_owner(tmp_path, attached_array):
    import numpy as np

    context, result, values, _hydration = attached_array
    destination = tmp_path / "export"
    exported = context.export_result(result, destination=destination)
    values.append(exported)
    manifest = _load_manifest(destination / "manifest.json")
    assets = _verify_tree(destination, manifest)
    supplied = SharedMemoryContext(tmp_path / "caller-owner")
    try:
        with supplied.activate():
            with create_shared_output(np.full((1,), 37, dtype=np.uint16)) as sentinel:
                pass
        loaded = load_public_return_from_bundle(
            destination,
            manifest["return_manifest"],
            assets,
            shared_memory_context=supplied,
        )
        values.append(loaded)
        _assert_pixels(loaded.at["0", "image"])
        [group] = result_groups(loaded)
        group.release()
        with open_shared_array(sentinel) as pixels:
            assert pixels.tolist() == [37]
        del pixels
        assert Path(supplied.descriptor()["root"]).is_dir()
    finally:
        supplied.close()


@pytest.mark.shared_memory
@pytest.mark.parametrize("phase", ["admission", "allocation"])
def test_failed_hydration_preserves_primary_and_owned_cleanup_retry(
    tmp_path, attached_array, monkeypatch, phase,
):
    context, result, _values, hydration = attached_array
    primary = RuntimeError(f"controlled {phase} refusal")
    observed_roots = []
    interrupted = False
    real_task_scope = SharedMemoryContext.task_scope
    real_create = SharedMemoryContext.create
    real_close = SharedMemoryContext.close

    class CleanupInterrupted(BaseException):
        pass

    def task_scope(scope, task_id):
        if Path(scope.descriptor()["owner_root"]).is_relative_to(hydration):
            observed_roots.append(scope)
            if phase == "admission":
                raise primary
        return real_task_scope(scope, task_id)

    def create(scope, data, name=None):
        if Path(scope.descriptor()["owner_root"]).is_relative_to(hydration):
            raise primary
        return real_create(scope, data, name=name)

    def close(scope):
        nonlocal interrupted
        if observed_roots and scope is observed_roots[0] and not interrupted:
            interrupted = True
            raise CleanupInterrupted("controlled cleanup interruption")
        return real_close(scope)

    monkeypatch.setattr(SharedMemoryContext, "task_scope", task_scope)
    monkeypatch.setattr(SharedMemoryContext, "create", create)
    monkeypatch.setattr(SharedMemoryContext, "close", close)
    try:
        with pytest.raises(RuntimeError) as failure:
            context.export_result(result, destination=tmp_path / "export")
        assert failure.value is primary
        [owned_root] = observed_roots
        assert primary.result_cleanup_scopes == (owned_root,)
        assert primary.result_cleanup_errors == (
            "CleanupInterrupted: controlled cleanup interruption",
        )
        root = Path(owned_root.descriptor()["owner_root"])
        assert root.is_dir()
        assert owned_root.status().state == "closed"
        assert not root.exists()
        _assert_pixels(result.at["0", "image"])
    finally:
        interrupted = True
        for owned_root in observed_roots:
            owned_root.close()


@pytest.mark.shared_memory
def test_bundle_only_export_installs_the_same_bundle_without_hydration(
    tmp_path, attached_array,
):
    context, result, _values, hydration = attached_array
    destination = tmp_path / "bundle"
    assert not (hydration / "bioimageflow-return-shared").exists()
    installed = context.export_result_bundle(result, destination=destination)
    assert installed == destination
    first = _load_manifest(installed / "manifest.json")
    _verify_tree(installed, first)
    repeated = context.export_result_bundle(result, destination=destination)
    assert repeated == installed
    assert _load_manifest(installed / "manifest.json")["digest"] == first["digest"]
    assert not (hydration / "bioimageflow-return-shared").exists()
    _assert_pixels(result.at["0", "image"])
