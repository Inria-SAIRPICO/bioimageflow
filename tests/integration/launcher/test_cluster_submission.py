from __future__ import annotations

import json
import hashlib
import shutil
import threading
import uuid
from datetime import timedelta
from pathlib import Path

from bioimageflow import (
    LocalUpload,
    PSIJLaunchConfig,
    ParslConfigRef,
    PreLaunchScript,
    Workflow,
)
from bioimageflow.launcher.cluster_agent import run_agent
from bioimageflow.launcher.cluster_bundle import prepare_cluster_bundle
from bioimageflow.launcher.cluster_protocol import request
from bioimageflow.launcher.repository import LauncherRepository
from bioimageflow.parsl import (
    ExecutorBinding,
    ExecutorCapabilities,
    WorkerEnvironmentAttestation,
    WorkerSlotCapacity,
)
from bioimageflow.storage import canonical_json_bytes


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


def test_one_shot_agent_upload_commit_and_submit_are_idempotent(
    tmp_path: Path,
    monkeypatch,
) -> None:
    workflow = Workflow(storage_path=tmp_path / "results")
    with workflow:
        workflow.input("cluster_path", Path, id="cluster-path")
        workflow.input("uploaded_path", Path, id="uploaded-path")
    upload = tmp_path / "local image.tif"
    upload.write_bytes(b"pixels")
    staging = tmp_path / "transport"
    launches = []

    def fake_launch(control, launch, *, secret_refs):
        launches.append((control.run_id, launch, secret_refs))

    monkeypatch.setattr(
        "bioimageflow.launcher.backends.launch_orchestrator",
        fake_launch,
    )
    with prepare_cluster_bundle(
        workflow,
        inputs={
            "cluster_path": Path("/cluster/data/input.tif"),
            "uploaded_path": LocalUpload(upload),
        },
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
        base = {
            "manifest": bundle.manifest,
            "staging_root": staging.as_posix(),
        }
        allocated = _call(
            "allocate-upload",
            base,
            str(uuid.uuid4()),
        )
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
        submit_id = str(uuid.uuid4())
        submit_arguments = {**base, "object_path": committed["object_path"]}
        first = _call("submit", submit_arguments, submit_id)
        second = _call("submit", submit_arguments, submit_id)

    assert first == second
    assert len(launches) == 1
    [run_id] = [first["run_id"]]
    control = LauncherRepository(workflow.storage_path).open(run_id)
    submission = control.read_submission()
    assert submission["storage_root"] == workflow.storage_path.as_posix()
    assert submission["run_id"] == run_id
    inputs_by_name = {
        item["name"]: item["value"] for item in submission["invocation"]["inputs"]
    }
    assert inputs_by_name["cluster_path"] == {
        "tag": "path",
        "value": "/cluster/data/input.tif",
    }
    installed = Path(inputs_by_name["uploaded_path"]["value"])
    assert installed.name == "local image.tif"
    assert "objects/sha256" in installed.as_posix()
    assert run_id not in installed.as_posix()


def test_concurrent_equal_submit_requests_launch_once(
    tmp_path: Path,
    monkeypatch,
) -> None:
    workflow = Workflow(storage_path=tmp_path / "results")
    staging = tmp_path / "transport"
    launches = 0
    launch_lock = threading.Lock()

    def fake_launch(control, launch, *, secret_refs):
        nonlocal launches
        with launch_lock:
            launches += 1

    monkeypatch.setattr(
        "bioimageflow.launcher.backends.launch_orchestrator",
        fake_launch,
    )
    with prepare_cluster_bundle(
        workflow,
        inputs=None,
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
        base = {
            "manifest": bundle.manifest,
            "staging_root": staging.as_posix(),
        }
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
        submit_id = str(uuid.uuid4())
        arguments = {**base, "object_path": committed["object_path"]}
        results = []

        def submit() -> None:
            results.append(_call("submit", arguments, submit_id))

        threads = [threading.Thread(target=submit) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

    assert len(results) == 2
    assert results[0] == results[1]
    assert launches == 1


def test_retry_after_crash_between_allocation_and_dispatch_resumes_once(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import bioimageflow.launcher.submission as submission_module

    workflow = Workflow(storage_path=tmp_path / "results")
    staging = tmp_path / "transport"
    launches = []

    def fake_launch(control, launch, *, secret_refs):
        launches.append(control.run_id)

    monkeypatch.setattr(
        "bioimageflow.launcher.backends.launch_orchestrator",
        fake_launch,
    )
    original_launch_prepared = submission_module._launch_prepared_control

    def crash_after_allocation(*args, **kwargs):
        raise KeyboardInterrupt("injected process crash")

    with prepare_cluster_bundle(
        workflow,
        inputs=None,
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
        base = {
            "manifest": bundle.manifest,
            "staging_root": staging.as_posix(),
        }
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
        submit_id = str(uuid.uuid4())
        arguments = {**base, "object_path": committed["object_path"]}
        monkeypatch.setattr(
            submission_module,
            "_launch_prepared_control",
            crash_after_allocation,
        )
        encoded = canonical_json_bytes(
            request("submit", arguments, request_id=submit_id)
        )
        try:
            run_agent(encoded)
        except KeyboardInterrupt:
            pass
        else:
            raise AssertionError("Injected crash was not observed.")
        monkeypatch.setattr(
            submission_module,
            "_launch_prepared_control",
            original_launch_prepared,
        )

        resumed = _call("submit", arguments, submit_id)
        repeated = _call("submit", arguments, submit_id)

    assert resumed == repeated
    assert launches == [resumed["run_id"]]


def test_retry_after_definitive_launch_failure_does_not_become_success(
    tmp_path: Path,
    monkeypatch,
) -> None:
    workflow = Workflow(storage_path=tmp_path / "results")
    staging = tmp_path / "transport"
    launches = 0

    def fail_launch(control, launch, *, secret_refs):
        nonlocal launches
        launches += 1
        raise RuntimeError("scheduler rejected submission")

    monkeypatch.setattr(
        "bioimageflow.launcher.backends.launch_orchestrator",
        fail_launch,
    )
    with prepare_cluster_bundle(
        workflow,
        inputs=None,
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
        base = {
            "manifest": bundle.manifest,
            "staging_root": staging.as_posix(),
        }
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
        submit_id = str(uuid.uuid4())
        encoded = canonical_json_bytes(
            request(
                "submit",
                {**base, "object_path": committed["object_path"]},
                request_id=submit_id,
            )
        )

        first = json.loads(run_agent(encoded))
        second = json.loads(run_agent(encoded))

    assert first["ok"] is False
    assert second["ok"] is False
    assert first["error"]["code"] == "remote-submission-failed"
    assert second["error"]["code"] == "remote-submission-failed"
    assert launches == 1


def test_cluster_pre_launch_is_snapshotted_before_idempotent_dispatch(
    tmp_path: Path,
    monkeypatch,
) -> None:
    workflow = Workflow(storage_path=tmp_path / "results")
    staging = tmp_path / "transport"
    source = tmp_path / "site init.sh"
    content = b"export SITE_ENV=ready\n"
    source.write_bytes(content)
    expected = f"sha256:{hashlib.sha256(content).hexdigest()}"
    observed = []

    def fake_launch(control, launch, *, secret_refs):
        del launch, secret_refs
        metadata = control.read_submission()["psij_pre_launch"]
        artifact = control.confined_path(metadata["artifact"]["path"])
        observed.append((metadata, artifact.read_bytes()))

    monkeypatch.setattr(
        "bioimageflow.launcher.backends.launch_orchestrator",
        fake_launch,
    )
    with prepare_cluster_bundle(
        workflow,
        inputs=None,
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
        pre_launch=PreLaunchScript.from_cluster_file(
            source.as_posix(),
            expected_digest=expected,
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
        submit_id = str(uuid.uuid4())
        arguments = {**base, "object_path": committed["object_path"]}
        first = _call("submit", arguments, submit_id)
        source.write_bytes(b"changed after dispatch\n")
        second = _call("submit", arguments, submit_id)

    assert first == second
    assert len(observed) == 1
    metadata, installed = observed[0]
    assert installed == content
    assert metadata["source_kind"] == "cluster_file"
    assert metadata["source_path"] == source.as_posix()
    assert metadata["expected_digest"] == expected
    assert metadata["artifact"]["digest"] == expected
