from __future__ import annotations

import copy
import json
from pathlib import Path

import pandas as pd
import pytest

from bioimageflow import DataFrameTool, Workflow
from bioimageflow.launcher.errors import LauncherProtocolError
from bioimageflow.launcher.inputs import (
    INVOCATION_SCHEMA,
    decode_typed_constant,
    encode_typed_constant,
    load_invocation,
    serialize_invocation,
)
from bioimageflow.storage import canonical_dataframe_identity
from bioimageflow.storage.dataframe_transport import (
    read_dataframe_transport,
    write_dataframe_transport,
)
from bioimageflow_core import IOModel


class _Source(DataFrameTool):
    accepts_upstream = False

    class Inputs(IOModel):
        value: int

    class Outputs(IOModel):
        value: int

    def transform(self, df, arguments):
        return pd.DataFrame({"value": [arguments.value]}, index=["row"])


def _workflow(tmp_path: Path) -> Workflow:
    workflow = Workflow(storage_path=tmp_path / "results", name="example")
    with workflow:
        workflow.input("settings", dict, id="input-settings")
        workflow.input("table", kind="dataframe", id="input-table")
        first = _Source()(value=1, name="first")
        _Source()(value=2, name="second")
        workflow.output("published", first["value"], id="output-published")
    return workflow


def test_typed_constants_round_trip_without_losing_paths(
    tmp_path: Path,
) -> None:
    path = tmp_path / "a" / ".." / "asset.tif"
    value = {
        ("key", 2): [
            None,
            True,
            7,
            1.25,
            "text",
            path,
            (False, {"nested": path}),
        ]
    }

    encoded = encode_typed_constant(value)
    decoded = decode_typed_constant(encoded)

    expected = (tmp_path / "asset.tif").resolve()
    assert decoded[("key", 2)][5] == expected
    assert type(decoded[("key", 2)][5]) is type(expected)
    assert decoded[("key", 2)][6] == (False, {"nested": expected})
    json.dumps(encoded, allow_nan=False)


@pytest.mark.parametrize(
    "value",
    [
        float("nan"),
        float("inf"),
        float("-inf"),
        object(),
        b"pickle-like",
    ],
)
def test_typed_constant_encoder_rejects_unsafe_values(value: object) -> None:
    with pytest.raises((TypeError, ValueError)):
        encode_typed_constant(value)


@pytest.mark.parametrize(
    "envelope",
    [
        {"tag": "pickle", "value": "payload"},
        {"tag": "float", "value": float("nan")},
        {"tag": "int", "value": True},
        {"tag": "path", "value": "../relative"},
        {"tag": "str", "value": "ok", "extra": True},
        {
            "tag": "dict",
            "value": [
                {
                    "key": {"tag": "list", "value": []},
                    "value": {"tag": "none", "value": None},
                }
            ],
        },
    ],
)
def test_typed_constant_decoder_fails_closed(envelope: object) -> None:
    with pytest.raises(ValueError):
        decode_typed_constant(envelope)


def test_typed_constant_encoder_rejects_cycles() -> None:
    cyclic: list[object] = []
    cyclic.append(cyclic)

    with pytest.raises(ValueError, match="cycles"):
        encode_typed_constant(cyclic)


def test_root_invocation_round_trips_constants_and_dataframe(
    tmp_path: Path,
) -> None:
    workflow = _workflow(tmp_path)
    absolute_asset = (tmp_path / "asset.tif").resolve()
    frame = pd.DataFrame(
        {
            "asset": [absolute_asset, absolute_asset.as_posix()],
            "score": [1.0, float("nan")],
        },
        index=pd.Index(["sample-1", "sample-2"], name="sample"),
    )
    control = tmp_path / "candidate"

    payload = serialize_invocation(
        workflow,
        inputs={
            "settings": {"output": Path("relative/output")},
            "table": frame,
        },
        control_candidate=control,
    )
    persisted = json.loads(json.dumps(payload, allow_nan=False))
    loaded = load_invocation(workflow, persisted, control_dir=control)

    assert payload["schema"] == INVOCATION_SCHEMA
    assert payload["variant"] == "root"
    assert [item["id"] for item in payload["inputs"]] == [
        "input-settings",
        "input-table",
    ]
    assert payload["outputs"] == [{"id": "output-published", "name": "published"}]
    assert "storage_path" not in json.dumps(payload)
    assert loaded.variant == "root"
    assert loaded.targets == ()
    assert loaded.outputs[0].port_id == "output-published"
    assert (
        loaded.inputs["settings"]["output"]
        == (Path.cwd() / "relative/output").resolve()
    )
    restored = loaded.inputs["table"]
    assert isinstance(restored, pd.DataFrame)
    assert isinstance(restored.iloc[0]["asset"], Path)
    assert type(restored.iloc[1]["asset"]) is str
    assert canonical_dataframe_identity(restored) == canonical_dataframe_identity(frame)

    dataframe_record = payload["inputs"][1]["dataframe"]
    assert dataframe_record["logical_digest"].startswith("sha256:")
    assert dataframe_record["transport_digest"].startswith("sha256:")
    assert dataframe_record["logical_digest"] != dataframe_record["transport_digest"]
    assert (control / dataframe_record["path"]).is_file()


@pytest.mark.parametrize(
    "frame",
    [
        pd.DataFrame({"value": [1, 2]}),
        pd.DataFrame(
            {"value": [1, 2]},
            index=pd.Index([10, 20], name="position"),
        ),
        pd.DataFrame(
            {"value": [1, 2]},
            index=pd.date_range(
                "2025-01-01",
                periods=2,
                tz="Europe/Paris",
                name="captured",
            ),
        ),
        pd.DataFrame(
            {"value": pd.Categorical(["a", "b"], ordered=True)},
            index=pd.MultiIndex.from_tuples(
                [("sample", 1), ("sample", 2)],
                names=["group", "position"],
            ),
        ),
    ],
    ids=["range", "integer", "datetime", "multi-index-categorical"],
)
def test_dataframe_transport_round_trips_index_metadata(
    tmp_path: Path,
    frame: pd.DataFrame,
) -> None:
    destination = tmp_path / "frame.parquet"

    metadata = write_dataframe_transport(frame, destination)
    restored = read_dataframe_transport(destination, metadata)

    assert [str(value) for value in restored.index] == [
        str(value) for value in frame.index
    ]
    assert list(restored.index.names) == list(frame.index.names)
    assert canonical_dataframe_identity(restored) == canonical_dataframe_identity(frame)


def test_root_loader_rejects_transport_and_logical_tampering(
    tmp_path: Path,
) -> None:
    workflow = _workflow(tmp_path)
    control = tmp_path / "candidate"
    payload = serialize_invocation(
        workflow,
        inputs={
            "settings": {},
            "table": pd.DataFrame({"value": [1]}, index=["row"]),
        },
        control_candidate=control,
    )
    dataframe = payload["inputs"][1]["dataframe"]
    transport = control / dataframe["path"]
    transport.write_bytes(transport.read_bytes() + b"tampered")

    with pytest.raises(LauncherProtocolError, match="failed verification"):
        load_invocation(workflow, payload, control_dir=control)

    clean_control = tmp_path / "clean"
    clean = serialize_invocation(
        workflow,
        inputs={
            "settings": {},
            "table": pd.DataFrame({"value": [1]}, index=["row"]),
        },
        control_candidate=clean_control,
    )
    clean["inputs"][1]["dataframe"]["logical_digest"] = "sha256:" + "0" * 64
    with pytest.raises(LauncherProtocolError, match="failed verification"):
        load_invocation(workflow, clean, control_dir=clean_control)


def test_root_loader_rejects_interface_drift_and_unknown_fields(
    tmp_path: Path,
) -> None:
    workflow = _workflow(tmp_path)
    control = tmp_path / "candidate"
    payload = serialize_invocation(
        workflow,
        inputs={"settings": {}},
        control_candidate=control,
    )

    renamed = copy.deepcopy(payload)
    renamed["inputs"][0]["name"] = "old-settings"
    with pytest.raises(LauncherProtocolError, match="current workflow interface"):
        load_invocation(workflow, renamed, control_dir=control)

    extra = copy.deepcopy(payload)
    extra["future"] = {}
    with pytest.raises(LauncherProtocolError, match="unknown fields"):
        load_invocation(workflow, extra, control_dir=control)

    future = copy.deepcopy(payload)
    future["schema"] = "bioimageflow.launcher.invocation.v2"
    with pytest.raises(LauncherProtocolError, match="unsupported"):
        load_invocation(workflow, future, control_dir=control)


def test_root_loader_rejects_escaping_and_symlinked_dataframe_paths(
    tmp_path: Path,
) -> None:
    workflow = _workflow(tmp_path)
    control = tmp_path / "candidate"
    payload = serialize_invocation(
        workflow,
        inputs={
            "settings": {},
            "table": pd.DataFrame({"value": [1]}, index=["row"]),
        },
        control_candidate=control,
    )

    escaping = copy.deepcopy(payload)
    escaping["inputs"][1]["dataframe"]["path"] = "../outside.parquet"
    with pytest.raises(LauncherProtocolError, match="unsafe"):
        load_invocation(workflow, escaping, control_dir=control)

    path = control / payload["inputs"][1]["dataframe"]["path"]
    original = tmp_path / "original.parquet"
    path.replace(original)
    path.symlink_to(original)
    with pytest.raises(LauncherProtocolError, match="symlink"):
        load_invocation(workflow, payload, control_dir=control)


def test_ad_hoc_targets_preserve_order_and_are_immediate(
    tmp_path: Path,
) -> None:
    workflow = _workflow(tmp_path)
    payload = serialize_invocation(
        workflow,
        targets=["second", "first"],
        control_candidate=tmp_path / "unused",
    )

    loaded = load_invocation(
        workflow,
        payload,
        control_dir=tmp_path,
    )

    assert payload == {
        "schema": INVOCATION_SCHEMA,
        "targets": ["second", "first"],
        "variant": "targets",
    }
    assert loaded.variant == "targets"
    assert loaded.targets == ("second", "first")
    assert dict(loaded.inputs) == {}


@pytest.mark.parametrize(
    "targets",
    [
        [],
        ["unknown"],
        ["first/internal"],
        ["first", "first"],
    ],
)
def test_ad_hoc_targets_reject_invalid_names(
    tmp_path: Path,
    targets: list[str],
) -> None:
    workflow = _workflow(tmp_path)

    with pytest.raises(ValueError):
        serialize_invocation(
            workflow,
            targets=targets,
            control_candidate=tmp_path / "unused",
        )


def test_inputs_and_targets_are_mutually_exclusive(tmp_path: Path) -> None:
    workflow = _workflow(tmp_path)

    with pytest.raises(ValueError, match="mutually exclusive"):
        serialize_invocation(
            workflow,
            inputs={},
            targets=["first"],
            control_candidate=tmp_path / "unused",
        )
