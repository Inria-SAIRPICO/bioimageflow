from pathlib import Path

import pandas as pd
import pytest

from bioimageflow.launcher.errors import WorkflowRunResultUnavailableError
from bioimageflow.launcher.returns import (
    load_public_return,
    persist_public_return,
)
from bioimageflow.storage import Storage, make_result_key
from bioimageflow.workflow.execution_context import ExecutionProviderOutcome
from tests.testkit.storage import _file_digest, _write_record


RUN_ID = "run_1234567812344abc923456789abcdef0"


def _control_dir(tmp_path: Path) -> Path:
    control = tmp_path / "launcher" / "v1" / "runs" / RUN_ID
    control.mkdir(parents=True)
    return control


def test_single_external_path_return_round_trips(tmp_path: Path) -> None:
    control = _control_dir(tmp_path)
    external = (tmp_path / "external.tif").resolve()
    value = pd.DataFrame(
        {"path": [external], "count": [3]},
        index=["row"],
    )

    persist_public_return(
        control,
        tmp_path,
        RUN_ID,
        value,
        outcomes=(),
        root_outputs=[{"port_id": "output-image", "name": "path"}],
    )
    loaded = load_public_return(control, tmp_path, RUN_ID)

    assert loaded.at["row", "path"] == external
    assert loaded.at["row", "count"] == 3


def test_mapping_shape_and_key_order_round_trip(tmp_path: Path) -> None:
    control = _control_dir(tmp_path)
    value = {
        "second": pd.DataFrame({"value": [2]}, index=["b"]),
        "first": pd.DataFrame({"value": [1]}, index=["a"]),
    }

    persist_public_return(
        control,
        tmp_path,
        RUN_ID,
        value,
        outcomes=(),
    )
    loaded = load_public_return(control, tmp_path, RUN_ID)

    assert list(loaded) == ["second", "first"]
    assert loaded["first"].at["a", "value"] == 1


def test_record_asset_return_uses_exact_immutable_record(tmp_path: Path) -> None:
    control = _control_dir(tmp_path)
    storage = Storage(tmp_path)
    result_key = make_result_key({"node": "writer"})
    output = {
        "path": "assets/mask.tif",
        "kind": "owned_asset",
        "asset_type": "file",
        "size": 4,
        "digest": _file_digest(b"mask"),
    }
    record_id = _write_record(storage, result_key, outputs=[output])
    asset = storage.resolve_record_asset(result_key, record_id, "assets/mask.tif")
    outcome = ExecutionProviderOutcome(
        node_key="writer",
        result_key=result_key,
        record_id=record_id,
        transient_invocation_id=None,
        path_columns=("mask",),
        owned_path_columns=("mask",),
        shared_array_columns=(),
    )

    persist_public_return(
        control,
        tmp_path,
        RUN_ID,
        pd.DataFrame({"renamed_mask": [asset]}, index=["row"]),
        outcomes=(outcome,),
    )
    loaded = load_public_return(control, tmp_path, RUN_ID)

    assert loaded.at["row", "renamed_mask"] == asset
    assert not (storage.result_dir(result_key) / "current.json").exists()


def test_pruned_record_raises_result_unavailable(tmp_path: Path) -> None:
    control = _control_dir(tmp_path)
    storage = Storage(tmp_path)
    result_key = make_result_key({"node": "writer"})
    output = {
        "path": "assets/mask.tif",
        "kind": "owned_asset",
        "asset_type": "file",
        "size": 4,
        "digest": _file_digest(b"mask"),
    }
    record_id = _write_record(storage, result_key, outputs=[output])
    asset = storage.resolve_record_asset(result_key, record_id, "assets/mask.tif")
    outcome = ExecutionProviderOutcome(
        node_key="writer",
        result_key=result_key,
        record_id=record_id,
        transient_invocation_id=None,
        path_columns=("mask",),
        owned_path_columns=("mask",),
        shared_array_columns=(),
    )
    persist_public_return(
        control,
        tmp_path,
        RUN_ID,
        pd.DataFrame({"mask": [asset]}, index=["row"]),
        outcomes=(outcome,),
    )
    record_dir = storage.result_dir(result_key) / "records" / record_id
    renamed = record_dir.with_name(f"{record_id}.pruned")
    record_dir.rename(renamed)

    with pytest.raises(
        WorkflowRunResultUnavailableError,
        match="Immutable record",
    ):
        load_public_return(control, tmp_path, RUN_ID)


def test_transient_return_asset_is_self_contained(tmp_path: Path) -> None:
    control = _control_dir(tmp_path)
    storage = Storage(tmp_path)
    invocation_id, invocation_dir, assets_dir = storage.create_transient_invocation(
        RUN_ID,
        "writer",
        engine="direct:sequential",
    )
    source = assets_dir / "mask.tif"
    source.write_bytes(b"transient-mask")
    storage.finish_transient_invocation(
        RUN_ID,
        "writer",
        invocation_id,
        status="succeeded",
    )
    outcome = ExecutionProviderOutcome(
        node_key="writer",
        result_key=None,
        record_id=None,
        transient_invocation_id=invocation_id,
        path_columns=("mask",),
        owned_path_columns=("mask",),
        shared_array_columns=(),
    )

    persist_public_return(
        control,
        tmp_path,
        RUN_ID,
        pd.DataFrame({"mask": [source]}, index=["row"]),
        outcomes=(outcome,),
    )
    moved = invocation_dir.with_name(f"{invocation_dir.name}.removed")
    invocation_dir.rename(moved)
    loaded = load_public_return(control, tmp_path, RUN_ID)

    returned = loaded.at["row", "mask"]
    assert isinstance(returned, Path)
    assert returned.read_bytes() == b"transient-mask"
    assert control in returned.parents
