"""Contract tests for v1 output/cache storage primitives."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pandas as pd
import pytest

from bioimageflow.storage_v1 import (
    CacheCorruptionError,
    CurrentPointer,
    RecordManifest,
    StorageV1,
    canonical_dataframe_digest,
    canonical_json_bytes,
    make_node_keys,
    make_result_key,
    make_record_id,
    result_shard_parts,
    validate_relative_posix_path,
)


def _file_digest(content: bytes) -> str:
    return f"sha256:{hashlib.sha256(content).hexdigest()}"


def _record_id_for(result_key: str, dataframe_digest: str, outputs: list[dict[str, object]]) -> str:
    return make_record_id(
        {
            "schema": "bioimageflow.cache.record.v1",
            "result_key": result_key,
            "dataframe": {
                "path": "dataframe.parquet",
                "digest": dataframe_digest,
            },
            "outputs": outputs,
        }
    )


def _write_record(
    storage: StorageV1,
    result_key: str,
    *,
    dataframe_digest: str | None = None,
    outputs: list[dict[str, object]] | None = None,
    dataframe_content: bytes = b"parquet",
) -> str:
    outputs = list(outputs or [])
    dataframe_digest = dataframe_digest or _file_digest(dataframe_content)
    record_id = _record_id_for(result_key, dataframe_digest, outputs)
    record_dir = storage.result_dir(result_key) / "records" / record_id
    record_dir.mkdir(parents=True)
    (record_dir / "dataframe.parquet").write_bytes(dataframe_content)
    for output in outputs:
        if output.get("kind") == "owned_asset":
            asset_path = record_dir / str(output["path"])
            asset_path.parent.mkdir(parents=True, exist_ok=True)
            asset_path.write_bytes(b"mask")
    manifest = RecordManifest(
        result_key=result_key,
        record_id=record_id,
        dataframe_digest=dataframe_digest,
        outputs=outputs,
    )
    (record_dir / "manifest.json").write_text(json.dumps(manifest.to_dict(), indent=2, sort_keys=True))
    return record_id


def test_make_node_keys_normalizes_and_disambiguates() -> None:
    keys = make_node_keys(["Segment", "segment", "sub/work", " café ", "cafe\u0301"])

    assert keys["Segment"].startswith("segment")
    assert keys["Segment"] != keys["segment"]
    assert "/" not in keys["sub/work"]
    assert keys[" café "] != keys["cafe\u0301"]
    assert all("/" not in key for key in keys.values())


@pytest.mark.parametrize(
    "segment",
    ["", ".", "..", "/abs", "a\\b", "CON", "NUL", "name\x00"],
)
def test_validate_relative_posix_path_rejects_unsafe_segments(segment: str) -> None:
    with pytest.raises(ValueError):
        validate_relative_posix_path(segment)


def test_validate_relative_posix_path_allows_nested_safe_paths() -> None:
    assert validate_relative_posix_path("assets/mask.tif") == "assets/mask.tif"


def test_result_key_and_shard_are_deterministic() -> None:
    material = {"tool": "Threshold", "params": {"value": 10}, "upstream": ["rec_1"]}

    key = make_result_key(material)

    assert key == make_result_key({"upstream": ["rec_1"], "params": {"value": 10}, "tool": "Threshold"})
    assert key.startswith("rk_")
    shard = result_shard_parts(key)
    assert shard == (key[3:5], key[5:7])


@pytest.mark.parametrize("result_key", ["rk_ab/cdef", "rk_abc..def", "xx_abcdef", "rk_ab"])
def test_result_shard_rejects_unsafe_result_keys(result_key: str) -> None:
    with pytest.raises(ValueError):
        result_shard_parts(result_key)


def test_canonical_json_bytes_are_stable() -> None:
    assert canonical_json_bytes({"b": 1, "a": [2, 3]}) == b'{"a":[2,3],"b":1}'


def test_canonical_dataframe_digest_is_stable_and_rejects_objects() -> None:
    df = pd.DataFrame(
        {
            "text": ["Cafe\u0301", "same"],
            "number": [1, 2],
            "float": [float("nan"), float("inf")],
        },
        index=["row1", "row2"],
    )

    digest = canonical_dataframe_digest(df)

    assert digest.startswith("sha256:")
    assert digest == canonical_dataframe_digest(df[["float", "number", "text"]])

    with pytest.raises(TypeError, match="Unsupported dataframe value"):
        canonical_dataframe_digest(pd.DataFrame({"bad": [object()]}))


def test_canonical_dataframe_digest_includes_schema_material() -> None:
    df = pd.DataFrame(
        {
            "b": pd.Series([1], dtype="uint64"),
            "a": [pd.Timestamp("2026-06-16T12:00:00+02:00")],
            "asset": ["assets/mask.tif"],
        }
    )

    digest = canonical_dataframe_digest(
        df,
        declared_columns=["b", "a"],
        column_kinds={"asset": "record_asset"},
    )

    assert digest == canonical_dataframe_digest(
        df[["asset", "a", "b"]],
        declared_columns=["b", "a"],
        column_kinds={"asset": "record_asset"},
    )
    assert digest != canonical_dataframe_digest(df)
    assert canonical_dataframe_digest(pd.DataFrame({"n": pd.Series([1], dtype="uint64")})) != canonical_dataframe_digest(
        pd.DataFrame({"n": pd.Series([1], dtype="int64")})
    )


def test_canonical_dataframe_digest_rejects_unsafe_record_asset_path() -> None:
    with pytest.raises(ValueError):
        canonical_dataframe_digest(
            pd.DataFrame({"asset": ["../escape.tif"]}),
            column_kinds={"asset": "record_asset"},
        )


def test_record_manifest_validation_checks_files_and_digest(tmp_path: Path) -> None:
    result_key = make_result_key({"node": "segment"})
    dataframe_digest = _file_digest(b"parquet")
    output = {"path": "assets/mask.tif", "kind": "owned_asset", "size": 4, "digest": _file_digest(b"not-mask")}
    record_id = _record_id_for(result_key, dataframe_digest, [output])
    record_dir = tmp_path / "records" / record_id
    record_dir.mkdir(parents=True)
    (record_dir / "dataframe.parquet").write_bytes(b"parquet")
    asset = record_dir / "assets" / "mask.tif"
    asset.parent.mkdir()
    asset.write_bytes(b"mask")

    manifest = RecordManifest(
        result_key=result_key,
        record_id=record_id,
        dataframe_digest=dataframe_digest,
        outputs=[output],
    )

    with pytest.raises(CacheCorruptionError, match="digest"):
        manifest.validate(record_dir)

    valid_output = {"path": "assets/mask.tif", "kind": "owned_asset", "size": 4, "digest": _file_digest(b"mask")}
    record_id = _record_id_for(result_key, dataframe_digest, [valid_output])
    record_dir = tmp_path / "records" / record_id
    record_dir.mkdir(parents=True)
    (record_dir / "dataframe.parquet").write_bytes(b"parquet")
    asset = record_dir / "assets" / "mask.tif"
    asset.parent.mkdir()
    asset.write_bytes(b"mask")
    manifest = RecordManifest(
        result_key=result_key,
        record_id=record_id,
        dataframe_digest=dataframe_digest,
        outputs=[valid_output],
    )
    manifest.validate(record_dir)

    bad_record_id = _record_id_for(result_key, dataframe_digest, [{"path": "../escape.tif", "kind": "owned_asset", "size": 1}])
    bad_record_dir = tmp_path / "records" / bad_record_id
    bad_record_dir.mkdir(parents=True)
    (bad_record_dir / "dataframe.parquet").write_bytes(b"parquet")
    bad_manifest = RecordManifest(
        result_key=result_key,
        record_id=bad_record_id,
        dataframe_digest=dataframe_digest,
        outputs=[{"path": "../escape.tif", "kind": "owned_asset", "size": 1}],
    )
    with pytest.raises(CacheCorruptionError):
        bad_manifest.validate(bad_record_dir)


def test_record_manifest_validation_rejects_symlink_asset_escape(tmp_path: Path) -> None:
    result_key = make_result_key({"node": "segment"})
    dataframe_digest = _file_digest(b"parquet")
    output = {"path": "assets/mask.tif", "kind": "owned_asset", "size": 4, "digest": _file_digest(b"mask")}
    record_id = _record_id_for(result_key, dataframe_digest, [output])
    record_dir = tmp_path / "records" / record_id
    record_dir.mkdir(parents=True)
    (record_dir / "dataframe.parquet").write_bytes(b"parquet")
    outside = tmp_path / "outside.tif"
    outside.write_bytes(b"mask")
    asset = record_dir / "assets" / "mask.tif"
    asset.parent.mkdir()
    asset.symlink_to(outside)

    manifest = RecordManifest(
        result_key=result_key,
        record_id=record_id,
        dataframe_digest=dataframe_digest,
        outputs=[output],
    )

    with pytest.raises(CacheCorruptionError, match="escapes"):
        manifest.validate(record_dir)


def test_record_manifest_validation_rejects_dataframe_symlink_escape(tmp_path: Path) -> None:
    result_key = make_result_key({"node": "segment"})
    dataframe_digest = _file_digest(b"parquet")
    record_id = _record_id_for(result_key, dataframe_digest, [])
    record_dir = tmp_path / "records" / record_id
    record_dir.mkdir(parents=True)
    outside = tmp_path / "outside.parquet"
    outside.write_bytes(b"parquet")
    (record_dir / "dataframe.parquet").symlink_to(outside)
    manifest = RecordManifest(
        result_key=result_key,
        record_id=record_id,
        dataframe_digest=dataframe_digest,
        outputs=[],
    )

    with pytest.raises(CacheCorruptionError, match="dataframe"):
        manifest.validate(record_dir)


def test_record_manifest_validation_checks_declared_dataframe_file_digest(tmp_path: Path) -> None:
    result_key = make_result_key({"node": "segment"})
    dataframe_digest = _file_digest(b"expected")
    record_id = _record_id_for(result_key, dataframe_digest, [])
    record_dir = tmp_path / "records" / record_id
    record_dir.mkdir(parents=True)
    (record_dir / "dataframe.parquet").write_bytes(b"actual")
    manifest = RecordManifest(
        result_key=result_key,
        record_id=record_id,
        dataframe_digest=dataframe_digest,
        outputs=[],
    )

    with pytest.raises(CacheCorruptionError, match="digest"):
        manifest.validate(record_dir)


def test_record_manifest_from_dict_rejects_non_mapping_and_unknown_fields() -> None:
    result_key = make_result_key({"node": "segment"})
    manifest = {
        "schema": "bioimageflow.cache.record.v1",
        "result_key": result_key,
        "record_id": _record_id_for(result_key, "sha256:" + "1" * 64, []),
        "dataframe": {"path": "dataframe.parquet", "digest": "sha256:" + "1" * 64},
        "outputs": [],
        "extra": "not allowed",
    }

    with pytest.raises(CacheCorruptionError):
        RecordManifest.from_dict([])
    with pytest.raises(CacheCorruptionError):
        RecordManifest.from_dict(manifest)


def test_make_record_id_excludes_execution_metadata() -> None:
    manifest = {
        "result_key": "rk_abcdef",
        "dataframe_digest": "sha256:" + "1" * 64,
        "outputs": [{"path": "assets/a.tif", "kind": "owned_asset", "size": 1}],
        "attempt_id": "attempt_a",
        "created_at": "today",
    }

    other = dict(manifest, attempt_id="attempt_b", created_at="tomorrow")

    assert make_record_id(manifest) == make_record_id(other)


def test_current_pointer_guarded_update_first_valid_policy(tmp_path: Path) -> None:
    storage = StorageV1(tmp_path)
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


def test_select_current_record_rejects_unsafe_record_id(tmp_path: Path) -> None:
    storage = StorageV1(tmp_path)
    result_key = make_result_key({"node": "segment"})

    with pytest.raises(ValueError):
        storage.select_current_record(
            result_key,
            candidate_record_id="../rec_escape",
            attempt_id="attempt",
            run_id="run",
        )


def test_select_current_record_rejects_invalid_manifest(tmp_path: Path) -> None:
    storage = StorageV1(tmp_path)
    result_key = make_result_key({"node": "segment"})
    record_id = make_record_id(
        {
            "schema": "bioimageflow.cache.record.v1",
            "result_key": result_key,
            "dataframe": {"path": "dataframe.parquet", "digest": "sha256:" + "1" * 64},
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


def test_select_current_record_rejects_symlinked_record_directory(tmp_path: Path) -> None:
    storage = StorageV1(tmp_path)
    result_key = make_result_key({"node": "segment"})
    record_id = _record_id_for(result_key, _file_digest(b"parquet"), [])
    outside_record = tmp_path / "outside-record"
    outside_record.mkdir()
    (outside_record / "dataframe.parquet").write_bytes(b"parquet")
    manifest = RecordManifest(
        result_key=result_key,
        record_id=record_id,
        dataframe_digest=_file_digest(b"parquet"),
        outputs=[],
    )
    (outside_record / "manifest.json").write_text(json.dumps(manifest.to_dict(), indent=2, sort_keys=True))
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


def test_load_current_raises_on_corrupt_pointer(tmp_path: Path) -> None:
    storage = StorageV1(tmp_path)
    result_key = make_result_key({"node": "segment"})
    result_dir = storage.result_dir(result_key)
    result_dir.mkdir(parents=True)
    (result_dir / "current.json").write_text("{not json")

    with pytest.raises(CacheCorruptionError):
        storage.load_current(result_key)


def test_load_current_raises_on_non_mapping_pointer(tmp_path: Path) -> None:
    storage = StorageV1(tmp_path)
    result_key = make_result_key({"node": "segment"})
    result_dir = storage.result_dir(result_key)
    result_dir.mkdir(parents=True)
    (result_dir / "current.json").write_text("[]")

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
    storage = StorageV1(tmp_path)
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
    storage = StorageV1(tmp_path)
    result_key = make_result_key({"node": "segment"})
    record_id = _write_record(storage, result_key)
    selected = storage.select_current_record(
        result_key,
        candidate_record_id=record_id,
        attempt_id="attempt",
        run_id="run",
    )
    (storage.result_dir(result_key) / "records" / record_id / "dataframe.parquet").unlink()

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
