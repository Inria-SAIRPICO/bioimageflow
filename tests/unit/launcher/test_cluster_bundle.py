from __future__ import annotations

import json
import os
from datetime import timedelta
from pathlib import Path

import pandas as pd
import pytest

from bioimageflow import LocalUpload, PSIJLaunchConfig, ParslConfigRef, Workflow
from bioimageflow.launcher.cluster_bundle import prepare_cluster_bundle
from bioimageflow.parsl import (
    ExecutorBinding,
    ExecutorCapabilities,
    WorkerEnvironmentAttestation,
    WorkerSlotCapacity,
)


def _workflow(tmp_path: Path, *, settings_type: type = Path) -> Workflow:
    workflow = Workflow(storage_path=tmp_path / "cluster-results")
    with workflow:
        workflow.input("settings", settings_type, id="settings")
        workflow.input("table", kind="dataframe", id="table")
    return workflow


def _binding() -> ExecutorBinding:
    return ExecutorBinding(
        label="threads",
        environments=(
            WorkerEnvironmentAttestation(
                name="default",
                dependency_hash="0" * 64,
                allow_flexible_versions=False,
                core_requirement="bioimageflow-core==0.1.7",
            ),
        ),
        capabilities=ExecutorCapabilities(
            storage_modes=("shared_fs",),
            tool_origin_modes=("installed_module",),
            slot=WorkerSlotCapacity(cpu=1),
        ),
    )


def _prepare(tmp_path: Path, workflow: Workflow, inputs: dict):
    return prepare_cluster_bundle(
        workflow,
        inputs=inputs,
        targets=None,
        parsl_config=ParslConfigRef("tests.unit.launcher.config_factories:build", {}),
        executor_bindings={"threads": _binding()},
        node_routes=None,
        environment_routes=None,
        shared_runtime_root=None,
        task_policy=None,
        launch=PSIJLaunchConfig(
            executor="slurm",
            walltime=timedelta(minutes=10),
        ),
    )


def test_bundle_preserves_cluster_paths_strings_dataframe_and_upload(
    tmp_path: Path,
) -> None:
    source = tmp_path / "local files"
    source.mkdir()
    (source / "image one.tif").write_bytes(b"pixels")
    workflow = _workflow(tmp_path)
    frame = pd.DataFrame(
        {
            "path": [Path("/cluster/data/image.tif")],
            "text": ["/looks/like/a/path"],
        }
    )

    with _prepare(
        tmp_path,
        workflow,
        {"settings": LocalUpload(source), "table": frame},
    ) as bundle:
        request = json.loads((bundle.root / "request.json").read_text())
        assert request["storage_path"] == (tmp_path / "cluster-results").as_posix()
        assert "storage_path" not in json.dumps(request["workflow"])
        assert request["inputs"][0]["kind"] == "local_upload"
        assert request["inputs"][0]["root_name"] == "local files"
        assert request["inputs"][1]["kind"] == "dataframe"
        assert bundle.manifest["digest"].startswith("sha256:")
        assert all("launcher" not in entry["path"] for entry in bundle.manifest["entries"])


def test_bundle_rejects_upload_in_non_path_root_field(tmp_path: Path) -> None:
    source = tmp_path / "local.txt"
    source.write_text("content")

    with pytest.raises(TypeError, match="not allowed"):
        with _prepare(
            tmp_path,
            _workflow(tmp_path, settings_type=str),
            {"settings": LocalUpload(source)},
        ):
            pass


def test_bundle_rejects_relative_dataframe_paths_and_upload_cells(
    tmp_path: Path,
) -> None:
    workflow = _workflow(tmp_path)
    for value in (Path("relative.tif"), LocalUpload(tmp_path / "file")):
        frame = pd.DataFrame({"value": [value]})
        with pytest.raises((TypeError, ValueError)):
            with _prepare(tmp_path, workflow, {"table": frame}):
                pass


def test_bundle_never_reads_unmarked_path(tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist"
    workflow = _workflow(tmp_path)

    with _prepare(tmp_path, workflow, {"settings": Path("/cluster/missing")}) as bundle:
        request = json.loads((bundle.root / "request.json").read_text())

    assert request["inputs"][0]["value"] == {
        "tag": "path",
        "value": "/cluster/missing",
    }
    assert not missing.exists()


def test_bundle_rejects_symlink(tmp_path: Path) -> None:
    source = tmp_path / "upload"
    source.mkdir()
    (source / "A.txt").write_text("a")
    workflow = _workflow(tmp_path)

    os.symlink(source / "A.txt", source / "link")
    with pytest.raises(ValueError, match="symlinks"):
        with _prepare(tmp_path, workflow, {"settings": LocalUpload(source)}):
            pass
