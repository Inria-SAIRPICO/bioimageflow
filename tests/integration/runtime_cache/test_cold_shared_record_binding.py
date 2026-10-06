"""Cold record admission reuses only its own sealed shared-array values."""

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from bioimageflow import Workflow, WorkflowExecutionContext, result_groups
from bioimageflow.cache import dataframe_publish, processing_prepare_attempt, processing_publish
from bioimageflow.row_relation import ResultRelation, RowAssociation
from bioimageflow.storage import CacheCorruptionError, Storage
from bioimageflow_core import GENERAL_ENV, IOModel, ProcessingTool, RowConsumption, SharedArray, SharedMemoryContext
from bioimageflow_core.shm import open_shared_array


class SharedPassThrough(ProcessingTool):
    environment = GENERAL_ENV
    row_consumption = RowConsumption.MAPPED

    class Inputs(IOModel):
        pixels: SharedArray
        source_path: Path

    class Outputs(IOModel):
        pixels: SharedArray
        value: int
        source_path: Path

    def process_row(self, arguments):
        return self.Outputs(pixels=arguments.pixels, value=2**53 + 1, source_path=arguments.source_path)


def _files(owner_root):
    return {path.relative_to(owner_root): path.stat().st_size for path in owner_root.rglob("*.npy")}


def _pixels(reference):
    with open_shared_array(reference) as pixels:
        assert pixels.dtype == np.dtype("uint16") and pixels.shape == (2,)
        assert not pixels.flags.writeable
        return pixels.tolist()


def _frame(reference, *, nested=False):
    value = {"leaves": ([reference],), "label": "exact"} if nested else reference
    return pd.DataFrame({"pixels": pd.Series([value], index=["row"], dtype=object),
                         "value": [2**53 + 1]}, index=["row"])


def _publish_processing(storage_path, reference, run_suffix):
    run_id = "run_" + run_suffix * 32
    key, attempt, staging, assets = processing_prepare_attempt(
        storage_path, "shared", "same-computation", run_id=run_id,
        invocation_id="inv_" + run_suffix * 32, engine="direct", tool_identity="tests:Shared")
    relation = ResultRelation("dataframe", "source::shared", "source", (RowAssociation((), ("row",)),))
    return processing_publish(
        storage_path, "shared", "same-computation", _frame(reference), result_key=key,
        attempt_id=attempt, run_id=run_id, staging_dir=staging, staging_assets_dir=assets,
        path_columns=set(), owned_path_columns=set(), shared_array_columns={"pixels"},
        row_relation=relation.to_dict())


def test_cold_processing_keeps_two_backings_and_exact_record_value(tmp_path):
    owner_root = tmp_path / "owner"
    owner = SharedMemoryContext(owner_root)
    reader_owner = SharedMemoryContext(tmp_path / "reader")
    context = WorkflowExecutionContext(shared_memory_context=owner)
    source_path = tmp_path / "source.bin"
    source_path.write_bytes(b"source-exact")
    producer = owner.create(np.array([4, 5], dtype="uint16"))
    with open_shared_array(producer, writable=True) as producer_view:
        pass
    with Workflow(engine="direct", storage_path=tmp_path / "records", shared_memory_context=owner) as workflow:
        node = SharedPassThrough()(pixels=producer, source_path=source_path, name="shared")
    frame = exact = None
    try:
        frame = workflow.compute(node, run_context=context)
        assert len(_files(owner_root)) == 2, "one mutable producer and one sealed output; no cold hydration copy"
        assert frame.index.tolist() == ["0"] and frame.columns.tolist() == ["pixels", "value", "source_path"]
        reference = frame.at["0", "pixels"]
        assert reference.bound_owner.descriptor()["owner_id"] == owner.descriptor()["owner_id"]
        assert _pixels(reference) == [4, 5]
        assert frame.at["0", "value"] == 2**53 + 1 and frame.at["0", "source_path"] == str(source_path)
        producer_view[:] = 99
        assert _pixels(reference) == [4, 5]
        [outcome] = context.execution_outcomes
        storage = Storage(workflow.storage_path)
        manifest, stored, address = storage.load_record(outcome.result_key, outcome.record_id,
            path_columns={"source_path"}, shared_array_columns={"pixels"})
        assert manifest.result_key == outcome.result_key and manifest.record_id == outcome.record_id
        assert address == storage.result_dir(outcome.result_key) / "records" / outcome.record_id
        assert stored.index.tolist() == frame.index.tolist() and stored.columns.tolist() == frame.columns.tolist()
        assert stored.at["0", "value"] == 2**53 + 1 and stored.at["0", "source_path"] == str(source_path)
        output = next(item for item in manifest.outputs if item.get("asset_role") == "shared_array")
        assert stored.at["0", "pixels"] == output["path"]
        assert np.load(address / output["path"], allow_pickle=False).tolist() == [4, 5]
        assert {item["name"]: item["kind"] for item in manifest.dataframe_logical_schema} == {
            "pixels": "record_asset", "value": "scalar", "source_path": "external_path"}
        with reader_owner.activate():
            exact_manifest, exact, exact_address = storage.load_record(outcome.result_key, outcome.record_id,
                path_columns={"source_path"}, shared_array_columns={"pixels"}, hydrate_assets=True)
        assert exact_manifest.to_dict() == manifest.to_dict() and exact_address == address
        assert exact.index.tolist() == frame.index.tolist() and exact.columns.tolist() == frame.columns.tolist()
        assert _pixels(exact.at["0", "pixels"]) == [4, 5]
        assert exact.at["0", "source_path"] == str(source_path) and exact.at["0", "value"] == 2**53 + 1
        [group] = result_groups(frame)
        with open_shared_array(reference) as mapped:
            live = np.asarray(mapped)[1:]
        del mapped
        group.release()
        assert live.tolist() == [5] and not live.flags.writeable
        assert owner.status().pending_leases == 0 and owner.status().pending_grants == 0
        assert group.status().pending_readers > 0
        del live
        for exact_group in result_groups(exact):
            exact_group.release()
    finally:
        producer_view = None
        for returned in (frame, exact):
            if returned is not None:
                for group in result_groups(returned):
                    group.release()
        assert owner.close().state == "closed"
        assert reader_owner.close().state == "closed"


def test_losing_shared_candidate_returns_first_valid_pixels(tmp_path):
    owner = SharedMemoryContext(tmp_path / "owner")
    try:
        a = owner.publish(owner.create(np.array([4, 5], dtype="uint16")))
        b = owner.publish(owner.create(np.array([9, 9], dtype="uint16")))
        with owner.activate():
            first = _publish_processing(tmp_path / "records", a, "a")
            losing = _publish_processing(tmp_path / "records", b, "b")
        assert first.dataframe.at["row", "pixels"] == a
        assert losing.result_key == first.result_key and losing.record_id == first.record_id
        assert losing.record_dir == first.record_dir and losing.manifest.to_dict() == first.manifest.to_dict()
        assert losing.dataframe.index.tolist() == ["row"]
        assert losing.dataframe.at["row", "pixels"] != b
        assert _pixels(losing.dataframe.at["row", "pixels"]) == [4, 5]
        storage = Storage(tmp_path / "records")
        pointer = storage.load_current(first.result_key)
        assert pointer.record_id == first.record_id
        assert len(list((storage.result_dir(first.result_key) / "conflicts").glob("*.json"))) == 1
        assert len([path for path in (storage.result_dir(first.result_key) / "records").iterdir()
                    if not path.name.startswith(".")]) == 2
        for selection in (first, losing):
            for group in result_groups(selection.dataframe):
                group.release()
    finally:
        assert owner.close().state == "closed"


def test_corrupt_own_shared_asset_refuses_before_reuse(tmp_path, monkeypatch):
    owner_root = tmp_path / "owner"
    owner = SharedMemoryContext(owner_root)
    select = Storage.select_current_record
    corrupted = []

    def select_then_corrupt(storage, result_key, **kwargs):
        pointer = select(storage, result_key, **kwargs)
        assert pointer.record_id == kwargs["candidate_record_id"]
        manifest = storage.load_record_manifest(result_key, pointer.record_id)
        output = next(item for item in manifest.outputs if item.get("asset_role") == "shared_array")
        path = storage.result_dir(result_key) / "records" / pointer.record_id / output["path"]
        path.write_bytes(b"corrupt own emitted asset")
        corrupted.append(path)
        return pointer

    monkeypatch.setattr(Storage, "select_current_record", select_then_corrupt)
    try:
        sealed = owner.publish(owner.create(np.array([4, 5], dtype="uint16")))
        before = _files(owner_root)
        with owner.activate(), pytest.raises(CacheCorruptionError, match="size mismatch|digest mismatch"):
            _publish_processing(tmp_path / "records", sealed, "c")
        assert len(corrupted) == 1 and _files(owner_root) == before
        assert _pixels(sealed) == [4, 5] and owner.status().pending_leases == 0
    finally:
        assert owner.close().state == "closed"


def test_dataframe_nested_sealed_leaf_reuses_backing_after_exact_admission(tmp_path):
    owner_root = tmp_path / "owner"
    owner = SharedMemoryContext(owner_root)
    reader_owner = SharedMemoryContext(tmp_path / "reader")
    try:
        sealed = owner.publish(owner.create(np.array([4, 5], dtype="uint16")))
        before = _files(owner_root)
        with owner.activate():
            selected = dataframe_publish(tmp_path / "records", "nested", "signature", _frame(sealed, nested=True),
                run_id="run_" + "d" * 32, engine="direct", tool_identity="tests:Nested")
        assert _files(owner_root) == before
        payload = selected.dataframe.at["row", "pixels"]
        assert type(payload) is dict and type(payload["leaves"]) is tuple and type(payload["leaves"][0]) is list
        assert payload["leaves"][0][0] == sealed and _pixels(payload["leaves"][0][0]) == [4, 5]
        assert payload["label"] == "exact" and selected.dataframe.at["row", "value"] == 2**53 + 1
        with reader_owner.activate():
            manifest, exact, address = Storage(tmp_path / "records").load_record(
                selected.result_key, selected.record_id, hydrate_assets=True)
        assert manifest.to_dict() == selected.manifest.to_dict() and address == selected.record_dir
        assert exact.index.tolist() == ["row"] and exact.columns.tolist() == ["pixels", "value"]
        assert exact.at["row", "pixels"]["label"] == "exact"
        assert _pixels(exact.at["row", "pixels"]["leaves"][0][0]) == [4, 5]
        for frame in (selected.dataframe, exact):
            for group in result_groups(frame):
                group.release()
    finally:
        assert owner.close().state == "closed"
        assert reader_owner.close().state == "closed"
