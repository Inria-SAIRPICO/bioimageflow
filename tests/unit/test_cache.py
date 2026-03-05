"""Unit tests for bioimageflow.cache."""

import json
from enum import Enum
from pathlib import Path

import pytest

from bioimageflow.cache import (
    normalize_dependencies,
    compute_env_hash,
    deterministic_serialize,
    compute_signature_hash,
)


class TestNormalizeDependencies:

    def test_sorts_keys(self):
        result = normalize_dependencies({"b": "1", "a": "2"})
        assert list(result.keys()) == ["a", "b"]

    def test_strips_strings(self):
        result = normalize_dependencies({"pkg": " 1.0.0 "})
        assert result["pkg"] == "1.0.0"

    def test_sorts_and_strips_lists(self):
        result = normalize_dependencies({"pkgs": ["  b ", "a"]})
        assert result["pkgs"] == ["a", "b"]

    def test_non_string_passthrough(self):
        result = normalize_dependencies({"flag": True})
        assert result["flag"] is True


class TestComputeEnvHash:

    def test_deterministic(self):
        deps = {"numpy": "1.26", "scipy": "1.12"}
        assert compute_env_hash(deps) == compute_env_hash(deps)

    def test_order_independent(self):
        h1 = compute_env_hash({"a": "1", "b": "2"})
        h2 = compute_env_hash({"b": "2", "a": "1"})
        assert h1 == h2

    def test_different_deps_different_hash(self):
        h1 = compute_env_hash({"numpy": "1.26"})
        h2 = compute_env_hash({"numpy": "1.27"})
        assert h1 != h2


class TestDeterministicSerialize:

    def test_dict_sorted(self):
        s1 = deterministic_serialize({"b": 1, "a": 2})
        s2 = deterministic_serialize({"a": 2, "b": 1})
        assert s1 == s2

    def test_path_as_posix(self):
        result = deterministic_serialize({"p": Path("/a/b/c")})
        assert "/a/b/c" in result

    def test_set_sorted(self):
        result = deterministic_serialize({"s": {3, 1, 2}})
        parsed = json.loads(result)
        assert parsed["s"] == ["1", "2", "3"]

    def test_tuple_as_list(self):
        result = deterministic_serialize({"t": (1, 2)})
        parsed = json.loads(result)
        assert parsed["t"] == [1, 2]

    def test_enum_as_value(self):
        class Color(Enum):
            RED = "red"

        result = deterministic_serialize({"c": Color.RED})
        parsed = json.loads(result)
        assert parsed["c"] == "red"

    def test_unknown_type_raises(self):
        with pytest.raises(TypeError, match="Cannot serialize"):
            deterministic_serialize({"x": object()})


class TestComputeSignatureHash:

    def test_deterministic(self):
        h1 = compute_signature_hash("tool", "1.0", "envhash", {"k": "v"}, {"up": "h1"})
        h2 = compute_signature_hash("tool", "1.0", "envhash", {"k": "v"}, {"up": "h1"})
        assert h1 == h2

    def test_different_params_different_hash(self):
        h1 = compute_signature_hash("tool", "1.0", "envhash", {"k": "v"}, {})
        h2 = compute_signature_hash("tool", "1.0", "envhash", {"k": "w"}, {})
        assert h1 != h2

    def test_source_hash_changes_result(self):
        h1 = compute_signature_hash("tool", "1.0", "env", {}, {})
        h2 = compute_signature_hash("tool", "1.0", "env", {}, {}, source_hash="abc123")
        assert h1 != h2
