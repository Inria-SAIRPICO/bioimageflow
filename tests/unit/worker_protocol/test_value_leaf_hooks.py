"""One container authority with finite, explicitly owned leaf codecs."""

from pathlib import Path

import numpy as np
import pytest

from bioimageflow_core import (
    SharedMemoryContext,
    decode_processing_value,
    encode_processing_value,
)


def _box(value, *, is_key):
    return {"kind": "owned_leaf", "value": value, "key": is_key}


def _unbox(node, *, is_key):
    assert set(node) == {"kind", "value", "key"}
    assert node["kind"] == "owned_leaf" and node["key"] is is_key
    return node["value"]


def test_leaf_hooks_preserve_order_key_types_and_literal_container_kinds():
    keys = [None, True, 7, -0.5, "kind", b"bytes"]
    value = {key: ([key], (key, {"kind": "dict", "items": [4]})) for key in keys}
    encoded = encode_processing_value(value, encode_leaf=_box)
    decoded = decode_processing_value(encoded, decode_leaf=_unbox)
    assert [(type(key), key) for key in decoded] == [(type(key), key) for key in keys]
    assert decoded == value
    for key in keys:
        assert type(decoded[key]) is tuple
        assert type(decoded[key][0]) is list
        assert type(decoded[key][1][1]) is dict


def test_encode_hook_receives_original_native_and_bound_shared_leaves(tmp_path, monkeypatch):
    owner = SharedMemoryContext(root=tmp_path)
    native = np.arange(6, dtype="uint16").reshape(2, 3)[:, ::2]
    reference = owner.create(np.array([4], dtype="uint16"))
    path = Path(tmp_path) / "leaf.npy"
    observed = []

    def encode(value, *, is_key):
        if not is_key:
            observed.append(value)
        return _box(value, is_key=is_key)

    def forbid_open(*args, **kwargs):
        pytest.fail("Codec admission must not attach or open an array")

    try:
        monkeypatch.setattr(SharedMemoryContext, "open", forbid_open)
        encoded = encode_processing_value([native, reference, path], encode_leaf=encode)
        actual = decode_processing_value(encoded, decode_leaf=_unbox)
        assert observed[0] is native and actual[0] is native
        assert observed[1] is reference and actual[1] is reference
        assert actual[1].bound_owner is owner
        assert actual[2] is path
    finally:
        owner.close()


def test_custom_leaves_keep_numpy_scalar_precision_nonfinite_and_signed_zero():
    values = [np.uint64(2**63 + 17), np.int16(-3), np.complex64(1 + 2j),
              np.bool_(True), np.float32(np.inf), np.float64(np.nan), -0.0]
    actual = decode_processing_value(
        encode_processing_value(values, encode_leaf=_box), decode_leaf=_unbox,
    )
    for expected, value in zip(values, actual, strict=True):
        assert type(value) is type(expected)
        if np.isnan(expected):
            assert np.isnan(value)
        else:
            assert value == expected
    assert np.signbit(actual[-1])


@pytest.mark.parametrize("value", [object(), np.array([object()], dtype=object),
                                  np.array([1], dtype=np.dtype("u1", metadata={"unsafe": 1}))])
def test_encoder_cannot_hide_an_unsupported_live_leaf(value):
    called = []

    def encode(item, *, is_key):
        called.append(item)
        return 4

    with pytest.raises((TypeError, ValueError)):
        encode_processing_value(value, encode_leaf=encode)
    assert called == []


@pytest.mark.parametrize("value", [object(), [], (), {}, np.array([object()], dtype=object)])
def test_decoder_cannot_introduce_containers_or_opaque_leaves(value):
    def decode(node, *, is_key):
        return value

    with pytest.raises((TypeError, ValueError)):
        decode_processing_value({"kind": "owned_leaf"}, decode_leaf=decode)


def test_container_key_is_refused_before_leaf_callback_or_child_decoding():
    called = []

    def decode(node, *, is_key):
        called.append(node)
        pytest.fail("Malformed container key must not reach leaf I/O")

    node = {"kind": "dict", "items": [
        [{"kind": "list", "items": [{"kind": "asset"}]}, {"kind": "asset"}],
    ]}
    with pytest.raises(ValueError, match="container node"):
        decode_processing_value(node, decode_leaf=decode)
    assert called == []


def test_key_role_allows_asset_refusal_before_hydration():
    hydrated = []

    def decode(node, *, is_key):
        if is_key:
            raise ValueError("Asset leaves cannot be dictionary keys")
        hydrated.append(node)
        return Path("asset.npy")

    node = {"kind": "dict", "items": [[{"kind": "asset"}, {"kind": "asset"}]]}
    with pytest.raises(ValueError, match="cannot be dictionary keys"):
        decode_processing_value(node, decode_leaf=decode)
    assert hydrated == []


def test_decoded_key_must_remain_an_exact_primitive():
    def decode(node, *, is_key):
        return np.int64(4)

    with pytest.raises(ValueError, match="primitive keys"):
        decode_processing_value({"kind": "dict", "items": [[4, 9]]}, decode_leaf=decode)
    with pytest.raises(TypeError, match="primitive keys"):
        encode_processing_value({Path("key"): 4}, encode_leaf=_box)


def test_hooks_do_not_replace_container_cycle_or_duplicate_key_admission():
    cycle = []
    cycle.append(cycle)
    with pytest.raises(ValueError, match="Cyclic"):
        encode_processing_value(cycle, encode_leaf=_box)
    node = {"kind": "list", "items": []}
    node["items"].append(node)
    with pytest.raises(ValueError, match="Cyclic"):
        decode_processing_value(node, decode_leaf=_unbox)
    duplicates = {"kind": "dict", "items": [
        [_box(4, is_key=True), _box(1, is_key=False)],
        [_box(4, is_key=True), _box(9, is_key=False)],
    ]}
    with pytest.raises(ValueError, match="duplicate keys"):
        decode_processing_value(duplicates, decode_leaf=_unbox)


def test_encoder_leaf_cannot_claim_container_authority():
    def encode(value, *, is_key):
        return {"kind": "list", "items": [4]}

    with pytest.raises(ValueError, match="container node"):
        encode_processing_value(4, encode_leaf=encode)
