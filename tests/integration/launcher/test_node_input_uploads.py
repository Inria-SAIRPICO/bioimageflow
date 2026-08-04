from __future__ import annotations

import json
import shutil
import uuid
from datetime import timedelta
from pathlib import Path

from bioimageflow import (
    LocalUpload,
    PSIJLaunchConfig,
    ParslConfigRef,
    Workflow,
    deserialize_constant,
)
from bioimageflow.launcher.cluster_agent import run_agent
from bioimageflow.launcher.artifacts import build_error_payload
from bioimageflow.launcher.cluster_bundle import prepare_cluster_bundle
from bioimageflow.launcher.cluster_protocol import request
from bioimageflow.launcher.inputs import decode_typed_constant
from bioimageflow.launcher.repository import LauncherRepository
from bioimageflow.parsl import (
    ExecutorBinding,
    ExecutorCapabilities,
    WorkerEnvironmentAttestation,
    WorkerSlotCapacity,
)
from bioimageflow.storage import canonical_json_bytes
from bioimageflow_common_tools import Files


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


def _call(operation: str, arguments: dict, request_id: str) -> dict:
    encoded = canonical_json_bytes(request(operation, arguments, request_id=request_id))
    response = json.loads(run_agent(encoded))
    assert response["ok"] is True, response
    return response["result"]


def test_nested_node_upload_becomes_immutable_effective_graph_path(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "laptop-images"
    source.mkdir()
    image = source / "image.tif"
    image.write_bytes(b"validated pixels")
    child = Workflow(storage_path=tmp_path / "unused-child-results")
    with child:
        Files()(path=source, name="files")
    workflow = Workflow(storage_path=tmp_path / "cluster-results")
    with workflow:
        child(name="nested")
    staging = tmp_path / "transport"
    launches: list[str] = []

    def fake_launch(control, launch, *, secret_refs):
        del launch, secret_refs
        launches.append(control.run_id)

    monkeypatch.setattr(
        "bioimageflow.launcher.backends.launch_orchestrator",
        fake_launch,
    )
    with prepare_cluster_bundle(
        workflow,
        inputs={},
        targets=None,
        parsl_config=ParslConfigRef(
            "tests.unit.launcher.config_factories:build",
            {"workers": 1},
        ),
        executor_bindings={"threads": _binding()},
        node_routes=None,
        environment_routes=None,
        shared_runtime_root=None,
        task_policy=None,
        launch=PSIJLaunchConfig(
            executor="slurm",
            walltime=timedelta(minutes=10),
        ),
        node_input_overrides={
            "nested/files": {"path": LocalUpload(source)},
        },
    ) as bundle:
        image.write_bytes(b"changed after preparation")
        base = {"manifest": bundle.manifest, "staging_root": staging.as_posix()}
        allocated = _call("allocate-upload", base, str(uuid.uuid4()))
        shutil.copytree(
            bundle.root,
            Path(allocated["remote_root"]),
            dirs_exist_ok=True,
        )
        committed = _call(
            "commit-upload",
            {**base, "upload_id": allocated["upload_id"]},
            str(uuid.uuid4()),
        )
        submitted = _call(
            "submit",
            {**base, "object_path": committed["object_path"]},
            str(uuid.uuid4()),
        )

    assert launches == [submitted["run_id"]]
    submission = LauncherRepository(workflow.storage_path).open(
        submitted["run_id"]
    ).read_submission()
    root_graph = submission["workflow"]["payload"]
    nested = next(node for node in root_graph["nodes"] if node["name"] == "nested")
    files = next(
        node for node in nested["workflow"]["nodes"] if node["name"] == "files"
    )
    installed = deserialize_constant(files["constants"]["path"])

    assert isinstance(installed, Path)
    assert installed.is_dir()
    assert (installed / "image.tif").read_bytes() == b"validated pixels"
    assert "objects/sha256" in installed.as_posix()
    assert source.as_posix() not in json.dumps(submission["workflow"])

    parent_control = LauncherRepository(workflow.storage_path).open(submitted["run_id"])
    parent_control.commit_terminal(
        expected_revision=0,
        expected_claim_epoch=None,
        new_state="failed",
        error_payload=build_error_payload(
            submitted["run_id"],
            code="test-failure",
            error=RuntimeError("retry me"),
        ),
    )
    retry_plan = _call(
        "plan-retry",
        {
            "run_id": submitted["run_id"],
            "storage_path": workflow.storage_path.as_posix(),
            "recompute": None,
        },
        str(uuid.uuid4()),
    )
    retried = _call(
        "start-retry",
        {
            "storage_path": workflow.storage_path.as_posix(),
            "plan": retry_plan,
        },
        str(uuid.uuid4()),
    )
    retried_submission = LauncherRepository(workflow.storage_path).open(
        retried["run_id"]
    ).read_submission()
    retried_root = retried_submission["workflow"]["payload"]
    retried_nested = next(
        node for node in retried_root["nodes"] if node["name"] == "nested"
    )
    retried_files = next(
        node
        for node in retried_nested["workflow"]["nodes"]
        if node["name"] == "files"
    )
    retried_installed = deserialize_constant(retried_files["constants"]["path"])

    assert retried_submission["retry_plan"]["parent_run_id"] == submitted["run_id"]
    assert retried_installed == installed
    assert (retried_installed / "image.tif").read_bytes() == b"validated pixels"
    assert launches == [submitted["run_id"], retried["run_id"]]


def test_nested_local_uploads_in_root_path_list_are_resolved_in_order(
    tmp_path: Path,
    monkeypatch,
) -> None:
    first = tmp_path / "same" / "image.tif"
    second = tmp_path / "other" / "image.tif"
    first.parent.mkdir()
    second.parent.mkdir()
    first.write_bytes(b"first")
    second.write_bytes(b"second")
    workflow = Workflow(storage_path=tmp_path / "cluster-results")
    with workflow:
        workflow.input("files", list[Path], id="input-files")
    staging = tmp_path / "transport"
    monkeypatch.setattr(
        "bioimageflow.launcher.backends.launch_orchestrator",
        lambda control, launch, *, secret_refs: None,
    )

    with prepare_cluster_bundle(
        workflow,
        inputs={"files": [LocalUpload(first), LocalUpload(second)]},
        targets=None,
        parsl_config=ParslConfigRef(
            "tests.unit.launcher.config_factories:build",
            {"workers": 1},
        ),
        executor_bindings={"threads": _binding()},
        node_routes=None,
        environment_routes=None,
        shared_runtime_root=None,
        task_policy=None,
        launch=PSIJLaunchConfig(
            executor="slurm",
            walltime=timedelta(minutes=10),
        ),
    ) as bundle:
        base = {"manifest": bundle.manifest, "staging_root": staging.as_posix()}
        allocated = _call("allocate-upload", base, str(uuid.uuid4()))
        shutil.copytree(
            bundle.root,
            Path(allocated["remote_root"]),
            dirs_exist_ok=True,
        )
        committed = _call(
            "commit-upload",
            {**base, "upload_id": allocated["upload_id"]},
            str(uuid.uuid4()),
        )
        submitted = _call(
            "submit",
            {**base, "object_path": committed["object_path"]},
            str(uuid.uuid4()),
        )

    submission = LauncherRepository(workflow.storage_path).open(
        submitted["run_id"]
    ).read_submission()
    values = decode_typed_constant(submission["invocation"]["inputs"][0]["value"])

    assert [path.read_bytes() for path in values] == [b"first", b"second"]
    assert values[0] != values[1]
    assert all("objects/sha256" in path.as_posix() for path in values)
