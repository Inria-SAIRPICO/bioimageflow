"""Persisted leaf authority is checked before attachment, including key roles."""
import pytest

from bioimageflow.portable_cells import admit_record_cell, cell_text


def _asset(role="native_array"):
    return {"kind": "asset", "role": role, "path": "assets/native/value.npy"}


def _output():
    return {"kind": "owned_asset", "asset_role": "native_array", "path": "assets/native/value.npy",
            "array": {"column": "payload", "row_index": "0"}}


@pytest.mark.parametrize("corruption", ["key", "role", "cell", "later-leaf"])
def test_malformed_cell_refuses_before_any_asset_attachment(corruption):
    node = {"kind": "list", "items": [_asset()]}
    output = _output()
    if corruption == "key":
        node = {"kind": "dict", "items": [[_asset(), {"kind": "scalar", "type": "null", "value": None}]]}
    elif corruption == "role":
        node["items"][0]["role"] = "shared_array"
    elif corruption == "cell":
        output["array"]["row_index"] = "1"
    else:
        node["items"].append({"kind": "scalar", "type": "numpy", "dtype": "uint16", "value": "00"})
    attached = []
    with pytest.raises(ValueError):
        admit_record_cell(cell_text(node), [output], column="payload", row_index="0",
                          hydrate_asset=lambda entry, role: attached.append((entry, role)))
    assert attached == []


def test_repeated_owned_path_leaf_has_one_manifest_owner(tmp_path):
    import pandas as pd
    from bioimageflow.cache.assets import portable_cell_assets

    assets = tmp_path / "assets"
    assets.mkdir()
    path = assets / "exact.bin"
    path.write_bytes(b"owned")
    frame = pd.DataFrame({"payload": [(path, path)]}, index=["0"])
    stored, outputs, owned, kinds = portable_cell_assets(frame, assets)
    assert len(outputs) == len(owned) == 1
    assert kinds == {"payload": "portable_value"}
    decoded, referenced = admit_record_cell(stored.at["0", "payload"], outputs, column="payload", row_index="0")
    assert type(decoded) is tuple and decoded[0] == decoded[1]
    assert referenced == set()
