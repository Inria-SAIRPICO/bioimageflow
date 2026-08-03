"""Tests for ``serialize_constant`` / ``deserialize_constant``.

The constant serializer is the canonical wire format used inside the
``constants`` block of :meth:`Workflow.to_dict` output, and is exported
as public API for platform consumers.
"""

from pathlib import Path

from bioimageflow import serialize_constant, deserialize_constant


class TestSerializeConstant:
    def test_none_round_trip(self):
        # None must round-trip to None — never to the lossy string "None",
        # which silently turned into a literal path argument when a tool
        # received it as ``input_image``.
        assert serialize_constant(None) == {"__type__": "none", "value": None}
        assert deserialize_constant({"__type__": "none", "value": None}) is None

    def test_bool_round_trip(self):
        assert serialize_constant(True) == {"__type__": "bool", "value": True}
        assert deserialize_constant({"__type__": "bool", "value": True}) is True

    def test_int_round_trip(self):
        assert serialize_constant(42) == {"__type__": "int", "value": 42}
        assert deserialize_constant({"__type__": "int", "value": 42}) == 42

    def test_float_round_trip(self):
        assert serialize_constant(3.14) == {"__type__": "float", "value": 3.14}
        assert deserialize_constant({"__type__": "float", "value": 3.14}) == 3.14

    def test_list_round_trip(self):
        encoded = serialize_constant([1, Path("images/a.tif")])
        assert encoded == {
            "__type__": "list",
            "value": [
                {"__type__": "int", "value": 1},
                {"__type__": "path", "value": "images/a.tif"},
            ],
        }
        assert deserialize_constant(encoded) == [1, Path("images/a.tif")]

    def test_tuple_round_trip(self):
        encoded = serialize_constant((1, 2))
        assert encoded == {
            "__type__": "tuple",
            "value": [
                {"__type__": "int", "value": 1},
                {"__type__": "int", "value": 2},
            ],
        }
        out = deserialize_constant(encoded)
        assert out == (1, 2) and isinstance(out, tuple)

    def test_string_round_trip(self):
        assert serialize_constant("hello") == {"__type__": "str", "value": "hello"}
        assert deserialize_constant({"__type__": "str", "value": "hello"}) == "hello"

    def test_path_round_trip_is_lossless(self):
        out = serialize_constant(Path("/tmp/x"))
        assert out == {"__type__": "path", "value": "/tmp/x"}
        assert deserialize_constant(out) == Path("/tmp/x")

    def test_dict_round_trip_is_recursive(self):
        value = {"reference": Path("refs/atlas.tif")}
        assert deserialize_constant(serialize_constant(value)) == value

    def test_bool_does_not_become_int(self):
        # bool is a subclass of int in Python; ensure we don't misclassify.
        assert serialize_constant(False)["__type__"] == "bool"
        assert serialize_constant(0)["__type__"] == "int"


class TestDeserializeUnknownType:
    def test_unknown_type_is_rejected(self):
        import pytest

        with pytest.raises(ValueError, match="Unknown workflow constant"):
            deserialize_constant({"__type__": "exotic", "value": 7})
