"""Focused tests split from ``tests/integration/runtime_cache/test_source_processing.py``."""

import json


from pathlib import Path


import pandas as pd

import pytest

from bioimageflow import Workflow


from bioimageflow.cache import (
    processing_prepare_attempt,
    processing_publish,
)

from bioimageflow.storage import (
    CacheCorruptionError,
    Storage,
)


from tests.testkit.runtime_cache import (
    EscapingSourceAssetWriter,
    FailingSourceAssetWriter,
    SourceAssetWriter,
    UnsafeTemplateSource,
    _current_pointer_files,
    _planned_result_key,
)


def test_failed_source_processing_tool_does_not_publish_current(tmp_path: Path) -> None:
    storage_path = tmp_path / "results"

    with Workflow(engine="direct", storage_path=storage_path) as wf:
        node = FailingSourceAssetWriter()(text="partial")
        with pytest.raises(RuntimeError, match="boom"):
            wf.compute(node)

    assert _current_pointer_files(storage_path) == []


def test_source_processing_tool_rejects_templated_output_outside_staging(
    tmp_path: Path,
) -> None:
    storage_path = tmp_path / "results"

    with Workflow(engine="direct", storage_path=storage_path) as wf:
        node = EscapingSourceAssetWriter()(directory=tmp_path)
        with pytest.raises(CacheCorruptionError):
            wf.compute(node)

    assert _current_pointer_files(storage_path) == []


@pytest.mark.parametrize(
    "template", ["../outside.txt", "/absolute.txt", r"nested\\backslash.txt"]
)
def test_source_processing_tool_rejects_unsafe_output_template_before_execution(
    tmp_path: Path,
    template: str,
) -> None:
    storage_path = tmp_path / "results"
    UnsafeTemplateSource.executions = 0

    with Workflow(engine="direct", storage_path=storage_path) as wf:
        node = UnsafeTemplateSource()(output_templates={"mask": template})
        with pytest.raises(CacheCorruptionError):
            wf.compute(node)

    assert UnsafeTemplateSource.executions == 0
    assert _current_pointer_files(storage_path) == []


def test_source_processing_tool_rejects_symlinked_attempts_directory_before_execution(
    tmp_path: Path,
) -> None:
    storage_path = tmp_path / "results"
    outside = tmp_path / "outside-attempts"
    outside.mkdir()
    SourceAssetWriter.executions = 0

    with Workflow(engine="direct", storage_path=storage_path) as wf:
        node = SourceAssetWriter()(text="symlink-attempt")
        result_key = _planned_result_key(wf, node.name)
        attempts_dir = Storage(storage_path).result_dir(result_key) / "attempts"
        attempts_dir.parent.mkdir(parents=True)
        attempts_dir.symlink_to(outside)
        with pytest.raises(CacheCorruptionError):
            wf.compute(node)

    assert SourceAssetWriter.executions == 0
    assert list(outside.iterdir()) == []
    assert _current_pointer_files(storage_path) == []


def test_processing_tool_publish_rejects_symlinked_record_assets_before_writing(
    tmp_path: Path,
) -> None:
    storage_path = tmp_path / "results"
    node_name = "SourceAssetWriter_1"
    sig_hash = "sig"
    result_key, attempt_id, staging_dir, assets_dir = processing_prepare_attempt(
        storage_path,
        node_name,
        sig_hash,
    )
    source = assets_dir / "mask.txt"
    source.write_text("first")
    df = pd.DataFrame({"mask": [str(source)], "count": [5]}, index=["0"])
    first = processing_publish(
        storage_path,
        node_name,
        sig_hash,
        df,
        result_key=result_key,
        attempt_id=attempt_id,
        run_id="run_0123456789abcdef0123456789abcdef",
        staging_dir=staging_dir,
        staging_assets_dir=assets_dir,
        path_columns={"mask"},
        owned_path_columns={"mask"},
    )
    record_dir = Path(first.loc["0", "mask"]).parents[1]
    assets_record_dir = record_dir / "assets"
    import shutil

    shutil.rmtree(assets_record_dir)
    outside = tmp_path / "outside-record-assets"
    outside.mkdir()
    assets_record_dir.symlink_to(outside)

    with pytest.raises(CacheCorruptionError):
        processing_publish(
            storage_path,
            node_name,
            sig_hash,
            df,
            result_key=result_key,
            attempt_id=attempt_id,
            run_id="run_0123456789abcdef0123456789abcdef",
            staging_dir=staging_dir,
            staging_assets_dir=assets_dir,
            path_columns={"mask"},
            owned_path_columns={"mask"},
        )
    assert list(outside.iterdir()) == []


def test_processing_tool_publish_accepts_declared_zero_row_owned_asset(
    tmp_path: Path,
) -> None:
    storage_path = tmp_path / "results"
    node_name = "ZeroRowAssetWriter_1"
    sig_hash = "sig"
    result_key, attempt_id, staging_dir, assets_dir = processing_prepare_attempt(
        storage_path,
        node_name,
        sig_hash,
    )
    source = assets_dir / "mask_0.txt"
    source.write_text("blank")
    df = pd.DataFrame(columns=pd.Index(["mask", "count"]))

    result = processing_publish(
        storage_path,
        node_name,
        sig_hash,
        df,
        result_key=result_key,
        attempt_id=attempt_id,
        run_id="run_0123456789abcdef0123456789abcdef",
        staging_dir=staging_dir,
        staging_assets_dir=assets_dir,
        path_columns={"mask"},
        owned_path_columns={"mask"},
        declared_owned_artifact_paths=[("mask", "0", source)],
    )

    assert result.empty
    pointer = Storage(storage_path).load_current(result_key)
    assert pointer is not None
    record_dir = (
        Storage(storage_path).result_dir(result_key) / "records" / pointer.record_id
    )
    manifest = json.loads((record_dir / "manifest.json").read_text())
    assert manifest["outputs"] == [
        {
            "asset_type": "file",
            "digest": manifest["outputs"][0]["digest"],
            "kind": "owned_asset",
            "output_column": "mask",
            "path": "assets/mask_0.txt",
            "row_index": "0",
            "size": 5,
        }
    ]
    assert (record_dir / "assets" / "mask_0.txt").read_text() == "blank"


@pytest.mark.parametrize(
    "relative",
    ["work/rows/000000/value.txt", "work/batch/value.txt"],
)
def test_processing_tool_publish_rejects_work_paths_as_external_outputs(
    tmp_path: Path,
    relative: str,
) -> None:
    storage_path = tmp_path / "results"
    node_name = "WorkPathWriter_1"
    sig_hash = "sig"
    result_key, attempt_id, staging_dir, assets_dir = processing_prepare_attempt(
        storage_path,
        node_name,
        sig_hash,
    )
    work_output = staging_dir / relative
    work_output.parent.mkdir(parents=True, exist_ok=True)
    work_output.write_text("mutable")
    df = pd.DataFrame({"output": [str(work_output)]}, index=["0"])

    with pytest.raises(CacheCorruptionError, match="mutable work state"):
        processing_publish(
            storage_path,
            node_name,
            sig_hash,
            df,
            result_key=result_key,
            attempt_id=attempt_id,
            run_id="run_0123456789abcdef0123456789abcdef",
            staging_dir=staging_dir,
            staging_assets_dir=assets_dir,
            path_columns={"output"},
            owned_path_columns=set(),
        )

    assert Storage(storage_path).load_current(result_key) is None


def test_processing_tool_publish_rejects_overlapping_directory_and_child_assets(
    tmp_path: Path,
) -> None:
    storage_path = tmp_path / "results"
    node_name = "DirectoryTool_1"
    sig_hash = "sig"
    result_key, attempt_id, staging_dir, assets_dir = processing_prepare_attempt(
        storage_path,
        node_name,
        sig_hash,
    )
    directory = assets_dir / "dataset.zarr"
    directory.mkdir()
    child = directory / "0"
    child.write_text("chunk")
    (directory / "1").write_text("other")
    df = pd.DataFrame(
        {"directory": [str(directory)], "child": [str(child)]},
        index=["0"],
    )

    with pytest.raises(CacheCorruptionError, match="Overlapping owned asset paths"):
        processing_publish(
            storage_path,
            node_name,
            sig_hash,
            df,
            result_key=result_key,
            attempt_id=attempt_id,
            run_id="run_0123456789abcdef0123456789abcdef",
            staging_dir=staging_dir,
            staging_assets_dir=assets_dir,
            path_columns={"directory", "child"},
            owned_path_columns={"directory", "child"},
        )
