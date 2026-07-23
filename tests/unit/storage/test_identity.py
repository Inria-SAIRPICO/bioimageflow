"""Focused tests split from ``tests/unit/test_storage.py``."""

from __future__ import annotations


# ruff: noqa: F401

import hashlib

import json

import os

from pathlib import Path

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

    assert key == make_result_key(
        {"upstream": ["rec_1"], "params": {"value": 10}, "tool": "Threshold"}
    )
    assert key.startswith("rk_")
    shard = result_shard_parts(key)
    assert shard == (key[3:5], key[5:7])


@pytest.mark.parametrize(
    "result_key", ["rk_ab/cdef", "rk_abc..def", "xx_abcdef", "rk_ab"]
)
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
    assert canonical_dataframe_digest(
        pd.DataFrame({"n": pd.Series([1], dtype="uint64")})
    ) != canonical_dataframe_digest(pd.DataFrame({"n": pd.Series([1], dtype="int64")}))


def test_canonical_dataframe_digest_rejects_unsafe_record_asset_path() -> None:
    with pytest.raises(ValueError):
        canonical_dataframe_digest(
            pd.DataFrame({"asset": ["../escape.tif"]}),
            column_kinds={"asset": "record_asset"},
        )


def test_canonical_scalar_payload_matches_dataframe_cell_encoding() -> None:
    assert canonical_scalar_payload(0) == {"kind": "signed_integer", "value": "0"}
    assert canonical_scalar_payload("Cafe\u0301") == {
        "kind": "string",
        "value": "Caf\u00e9",
    }
    assert canonical_scalar_payload(None) == {"kind": "null", "value": None}
