"""Tests for ``serialize_constant`` / ``deserialize_constant``.

The constant serializer is the canonical wire format used inside the
``constants`` block of :meth:`Workflow.to_dict` output, and is exported
as public API for platform consumers.
"""

from pathlib import Path

from bioimageflow import serialize_constant, deserialize_constant


class TestSerializeConstant:
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
        assert serialize_constant([1, 2, 3]) == {
            "__type__": "list", "value": [1, 2, 3],
        }
        assert deserialize_constant({"__type__": "list", "value": [1, 2, 3]}) == [
            1, 2, 3,
        ]

    def test_tuple_round_trip(self):
        assert serialize_constant((1, 2)) == {"__type__": "tuple", "value": [1, 2]}
        out = deserialize_constant({"__type__": "tuple", "value": [1, 2]})
        assert out == (1, 2) and isinstance(out, tuple)

    def test_string_round_trip(self):
        assert serialize_constant("hello") == {"__type__": "str", "value": "hello"}
        assert deserialize_constant({"__type__": "str", "value": "hello"}) == "hello"

    def test_path_is_lossy_str_fallback(self):
        # Path is documented as lossy: it goes through the str() fallback.
        out = serialize_constant(Path("/tmp/x"))
        assert out == {"__type__": "str", "value": "/tmp/x"}
        # Round-trip lands on a string, not a Path.
        assert deserialize_constant(out) == "/tmp/x"

    def test_bool_does_not_become_int(self):
        # bool is a subclass of int in Python; ensure we don't misclassify.
        assert serialize_constant(False)["__type__"] == "bool"
        assert serialize_constant(0)["__type__"] == "int"


class TestDeserializeUnknownType:
    def test_unknown_type_falls_back_to_str(self):
        # An unrecognized __type__ is coerced to str rather than raising —
        # this lets the wire format evolve without a hard break.
        assert deserialize_constant({"__type__": "exotic", "value": 7}) == "7"
