"""Focused tests split from ``tests/unit/test_storage.py``."""

from __future__ import annotations


# ruff: noqa: F401

import hashlib

import json

import os

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pandas as pd

import pytest

from bioimageflow.storage import (
    CacheCorruptionError,
    CurrentPointer,
    OutputViewCapability,
    RecordManifest,
    Storage,
    canonical_dataframe_digest,
    canonical_json_bytes,
    canonical_scalar_payload,
    make_node_keys,
    make_result_key,
    make_record_id,
    result_shard_parts,
    validate_relative_posix_path,
)


from tests.testkit.storage import (
    _file_digest,
    _record_id_for,
    _write_record,
)


def test_current_pointer_guarded_update_first_valid_policy(tmp_path: Path) -> None:
    storage = Storage(tmp_path)
    result_key = make_result_key({"node": "segment"})
    result_dir = storage.result_dir(result_key)
    record_a = _write_record(storage, result_key)

    selected = storage.select_current_record(
        result_key,
        candidate_record_id=record_a,
        attempt_id="attempt_a",
        run_id="run_a",
    )

    assert selected.record_id == record_a
    current = json.loads((result_dir / "current.json").read_text())
    assert current["policy"] == "first-valid"

    record_b = _write_record(storage, result_key, dataframe_content=b"other-parquet")
    selected_again = storage.select_current_record(
        result_key,
        candidate_record_id=record_b,
        attempt_id="attempt_b",
        run_id="run_b",
    )

    assert selected_again.record_id == record_a
    conflicts = list((result_dir / "conflicts").glob("*.json"))
    assert len(conflicts) == 1


def test_concurrent_first_valid_selection_returns_one_selected_record(
    tmp_path: Path,
) -> None:
    storage = Storage(tmp_path)
    result_key = make_result_key({"node": "concurrent-segment"})
    record_a = _write_record(storage, result_key)
    record_b = _write_record(storage, result_key, dataframe_content=b"other-parquet")
    barrier = Barrier(2)

    def select(record_id: str, suffix: str) -> CurrentPointer:
        barrier.wait(timeout=5)
        return storage.select_current_record(
            result_key,
            candidate_record_id=record_id,
            attempt_id=f"attempt_{suffix}",
            run_id=f"run_{suffix}",
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        selections = [
            future.result(timeout=5)
            for future in (
                pool.submit(select, record_a, "a"),
                pool.submit(select, record_b, "b"),
            )
        ]

    selected_ids = {selection.record_id for selection in selections}
    assert len(selected_ids) == 1
    assert storage.load_current(result_key).record_id in {record_a, record_b}
    conflicts = list(
        (storage.result_dir(result_key) / "conflicts").glob("*.json")
    )
    assert len(conflicts) == 1
    conflict = json.loads(conflicts[0].read_text())
    assert {
        conflict["current_record_id"],
        conflict["candidate_record_id"],
    } == {record_a, record_b}


def test_select_current_record_rejects_unsafe_record_id(tmp_path: Path) -> None:
    storage = Storage(tmp_path)
    result_key = make_result_key({"node": "segment"})

    with pytest.raises(ValueError):
        storage.select_current_record(
            result_key,
            candidate_record_id="../rec_escape",
            attempt_id="attempt",
            run_id="run",
        )


def test_select_current_record_rejects_invalid_manifest(tmp_path: Path) -> None:
    storage = Storage(tmp_path)
    result_key = make_result_key({"node": "segment"})
    record_id = make_record_id(
        {
            "schema": "bioimageflow.cache.record.v1",
            "result_key": result_key,
            "dataframe": {
                "path": "dataframe.parquet",
                "format": "parquet",
                "logical_digest": "sha256:" + "1" * 64,
                "transport_digest": "sha256:" + "1" * 64,
                "logical_schema": [],
            },
            "outputs": [],
        }
    )
    record_dir = storage.result_dir(result_key) / "records" / record_id
    record_dir.mkdir(parents=True)
    (record_dir / "manifest.json").write_text("{}")

    with pytest.raises(CacheCorruptionError):
        storage.select_current_record(
            result_key,
            candidate_record_id=record_id,
            attempt_id="attempt",
            run_id="run",
        )


def test_select_current_record_rejects_symlinked_record_directory(
    tmp_path: Path,
) -> None:
    storage = Storage(tmp_path)
    result_key = make_result_key({"node": "segment"})
    record_id = _record_id_for(result_key, _file_digest(b"parquet"), [])
    outside_record = tmp_path / "outside-record"
    outside_record.mkdir()
    (outside_record / "dataframe.parquet").write_bytes(b"parquet")
    manifest = RecordManifest(
        result_key=result_key,
        record_id=record_id,
        dataframe_logical_digest=_file_digest(b"parquet"),
        dataframe_transport_digest=_file_digest(b"parquet"),
        dataframe_logical_schema=[],
        outputs=[],
    )
    (outside_record / "manifest.json").write_text(
        json.dumps(manifest.to_dict(), indent=2, sort_keys=True)
    )
    records_dir = storage.result_dir(result_key) / "records"
    records_dir.mkdir(parents=True)
    (records_dir / record_id).symlink_to(outside_record)

    with pytest.raises(CacheCorruptionError, match="escapes"):
        storage.select_current_record(
            result_key,
            candidate_record_id=record_id,
            attempt_id="attempt",
            run_id="run",
        )


@pytest.mark.parametrize(
    "pointer_payload",
    [
        pytest.param("{not json", id="corrupt-json"),
        pytest.param("[]", id="non-mapping"),
    ],
)
def test_load_current_raises_on_invalid_pointer_payload(
    tmp_path: Path, pointer_payload: str
) -> None:
    storage = Storage(tmp_path)
    result_key = make_result_key({"node": "segment"})
    result_dir = storage.result_dir(result_key)
    result_dir.mkdir(parents=True)
    (result_dir / "current.json").write_text(pointer_payload)

    with pytest.raises(CacheCorruptionError):
        storage.load_current(result_key)


def test_current_pointer_from_dict_rejects_invalid_selected_by() -> None:
    result_key = make_result_key({"node": "segment"})
    record_id = _record_id_for(result_key, "sha256:" + "1" * 64, [])
    pointer = CurrentPointer(
        result_key=result_key,
        record_id=record_id,
        manifest=f"records/{record_id}/manifest.json",
        attempt_id="attempt",
        run_id="run",
    ).to_dict()
    pointer["selected_by"] = "bad"

    with pytest.raises(CacheCorruptionError):
        CurrentPointer.from_dict(pointer)


def test_load_current_raises_on_invalid_manifest_path(tmp_path: Path) -> None:
    storage = Storage(tmp_path)
    result_key = make_result_key({"node": "segment"})
    result_dir = storage.result_dir(result_key)
    result_dir.mkdir(parents=True)
    record_id = _record_id_for(result_key, "sha256:" + "1" * 64, [])
    pointer = CurrentPointer(
        result_key=result_key,
        record_id=record_id,
        manifest=f"records/{record_id}/manifest.json",
        attempt_id="attempt",
        run_id="run",
    ).to_dict()
    pointer["manifest"] = "../escape/manifest.json"
    (result_dir / "current.json").write_text(json.dumps(pointer))

    with pytest.raises(CacheCorruptionError):
        storage.load_current(result_key)


def test_load_current_validates_selected_manifest(tmp_path: Path) -> None:
    storage = Storage(tmp_path)
    result_key = make_result_key({"node": "segment"})
    record_id = _write_record(storage, result_key)
    selected = storage.select_current_record(
        result_key,
        candidate_record_id=record_id,
        attempt_id="attempt",
        run_id="run",
    )
    (
        storage.result_dir(result_key) / "records" / record_id / "dataframe.parquet"
    ).unlink()

    with pytest.raises(CacheCorruptionError):
        storage.load_current(selected.result_key)


def test_current_pointer_round_trip() -> None:
    result_key = make_result_key({"node": "segment"})
    record_id = _record_id_for(result_key, "sha256:" + "1" * 64, [])
    pointer = CurrentPointer(
        result_key=result_key,
        record_id=record_id,
        manifest=f"records/{record_id}/manifest.json",
        attempt_id="attempt",
        run_id="run",
    )

    assert CurrentPointer.from_dict(pointer.to_dict()).record_id == record_id
