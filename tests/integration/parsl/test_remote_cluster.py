from __future__ import annotations

from datetime import timedelta
from pathlib import Path, PurePosixPath

import pandas as pd
import pytest

from bioimageflow import (
    DataFrameTool,
    ExecutorBinding,
    ExecutorCapabilities,
    LocalUpload,
    PSIJLaunchConfig,
    ParslConfigRef,
    RemoteWorkflowRun,
    SSHSubmissionTransport,
    WorkerEnvironmentAttestation,
    WorkerSlotCapacity,
    Workflow,
    submit_workflow,
)
from bioimageflow.cache import compute_env_hash
from bioimageflow.launcher.errors import WorkflowRunFailedError
from bioimageflow.launcher.inputs import load_invocation
from bioimageflow.launcher.repository import LauncherRepository
from bioimageflow.parsl.startup import CORE_REQUIREMENT
from bioimageflow_core import Arguments, IOModel, ProcessingTool
from tests.testkit.parsl_tools import PARSL_TEST_ENV, ParslFail
from tests.testkit.remote_cluster import FakeCluster

pytestmark = pytest.mark.parsl


class _MetadataSource(DataFrameTool):
    accepts_upstream = False

    class Inputs(IOModel):
        uploaded: Path
        cluster_path: Path
        note: str

    class Outputs(IOModel):
        uploaded_size: int

    def transform(self, df, arguments):
        return pd.DataFrame(
            {"uploaded_size": [arguments.uploaded.stat().st_size]},
            index=["row"],
        )


class _RootBarrier(DataFrameTool):
    class Outputs(IOModel):
        value: int

    def transform(self, df, arguments):
        return pd.DataFrame({"value": pd.DataFrame(df)["value"]})


class _ArchiveIncrement(ProcessingTool):
    environment = PARSL_TEST_ENV

    class Inputs(IOModel):
        value: int

    class Outputs(IOModel):
        incremented: int

    def process_row(self, arguments: Arguments) -> "_ArchiveIncrement.Outputs":
        return self.Outputs(incremented=arguments.value + 1)


class _FinalBarrier(DataFrameTool):
    class Outputs(IOModel):
        incremented: int
        uploaded_size: int

    def transform(self, df, arguments):
        return pd.DataFrame(df)


def _binding() -> ExecutorBinding:
    return ExecutorBinding(
        label="threads",
        environments=(
            WorkerEnvironmentAttestation(
                name=PARSL_TEST_ENV.name,
                dependency_hash=compute_env_hash(PARSL_TEST_ENV.dependencies),
                allow_flexible_versions=False,
                core_requirement=CORE_REQUIREMENT,
            ),
        ),
        capabilities=ExecutorCapabilities(
            storage_modes=("shared_fs",),
            tool_origin_modes=("archive_module", "installed_module"),
            slot=WorkerSlotCapacity(cpu=1),
        ),
    )


def _transport(staging: Path) -> SSHSubmissionTransport:
    return SSHSubmissionTransport(
        host="test-cluster",
        staging_root=PurePosixPath(staging.as_posix()),
        remote_executable=PurePosixPath("/cluster/bin/bioimageflow-cluster-agent"),
    )


def _launch() -> PSIJLaunchConfig:
    return PSIJLaunchConfig(
        executor="slurm",
        walltime=timedelta(minutes=5),
    )


def _config(storage: Path) -> ParslConfigRef:
    return ParslConfigRef(
        "tests.unit.launcher.config_factories:build_threads",
        {"max_threads": 2, "run_dir": str(storage / "parsl-runinfo")},
    )


def test_remote_cluster_composes_transport_agent_launcher_parsl_and_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = tmp_path / "cluster-storage"
    staging = tmp_path / "transport"
    uploaded = tmp_path / "laptop image.bin"
    uploaded.write_bytes(b"pixels")
    cluster = FakeCluster(lose_submit_response=True)
    cluster.install(monkeypatch)
    original_archive_class = dict(_ArchiveIncrement.__dict__)

    workflow = Workflow(storage_path=storage, engine="direct")
    with workflow:
        table = workflow.input("table", kind="dataframe", id="table")
        upload = workflow.input("upload", Path, id="upload")
        cluster_path = workflow.input("cluster_path", Path, id="cluster-path")
        note = workflow.input("note", str, id="note")
        root = _RootBarrier()(table, name="root-barrier")
        processed = _ArchiveIncrement()(
            value=root["value"],
            name="processing",
        )
        metadata = _MetadataSource()(
            uploaded=upload,
            cluster_path=cluster_path,
            note=note,
            name="metadata",
        )
        final = _FinalBarrier()(
            root,
            processed,
            metadata,
            name="final-barrier",
        )
        workflow.output("incremented", final["incremented"], id="incremented")
        workflow.output(
            "uploaded_size",
            final["uploaded_size"],
            id="uploaded-size",
        )

    run = submit_workflow(
        workflow,
        inputs={
            "table": pd.DataFrame(
                {
                    "value": [2],
                    "dataframe_path": [Path("/cluster/data/from-frame.tif")],
                    "label": ["/looks/local/but-is-a-string"],
                },
                index=["row"],
            ),
            "upload": LocalUpload(uploaded),
            "cluster_path": Path("/cluster/data/reference.tif"),
            "note": "/looks/local/but-is-a-string",
        },
        parsl_config=_config(storage),
        executor_bindings={"threads": _binding()},
        shared_runtime_root=storage / "shared-runtime",
        launch=_launch(),
        transport=_transport(staging),
    )

    assert isinstance(run, RemoteWorkflowRun)
    assert run.status == "prepared"
    assert cluster.launch_count == 1
    assert cluster.operation_counts["submit"] == 2
    assert len(cluster.job_specs) == 1
    spec = cluster.job_specs[0]
    assert spec.arguments[-2:] == ["--run-id", run.id]
    queued = [event for event in run.progress() if event["kind"] == "backend"]
    queued_event = next(
        event for event in queued if event["payload"]["event"] == "psij_queued"
    )
    assert queued_event["payload"]["native_id"] == "fake-native-1"
    assert cluster.control is not None
    receipt = cluster.control.confined_path("psij_job.json")
    assert f'"run_id":"{run.id}"' in receipt.read_text(encoding="utf-8")
    assert '"native_id":"fake-native-1"' in receipt.read_text(encoding="utf-8")
    logs = cluster.control.control_dir / "logs"
    logs.mkdir(exist_ok=True)
    (logs / "orchestrator.out").write_bytes(b"queued stdout")
    (logs / "orchestrator.err").write_bytes(b"queued stderr")
    assert run.logs() == "[stdout]\nqueued stdout\n[stderr]\nqueued stderr"
    reconnected = RemoteWorkflowRun.open(
        _transport(staging), storage.as_posix(), run.id
    )
    assert cluster.run_queued_job() == "succeeded"
    reconnected.refresh()
    destination = tmp_path / "download" / run.id
    destination.parent.mkdir()
    original_download = cluster.download

    def disconnect_after_first_get(transport, commands):
        original_download(transport, commands)
        raise ConnectionError("injected SFTP disconnect")

    monkeypatch.setattr(
        "bioimageflow.launcher.result_download._run_sftp",
        disconnect_after_first_get,
    )
    with pytest.raises(ConnectionError, match="injected"):
        reconnected.result(destination=destination)
    assert not destination.exists()
    assert not list(destination.parent.glob(f".{run.id}.*.partial"))
    monkeypatch.setattr(
        "bioimageflow.launcher.result_download._run_sftp",
        original_download,
    )
    result = reconnected.result(destination=destination)

    assert list(result) == [
        "incremented",
        "uploaded_size",
    ]
    assert result["incremented"].at["row"] == 3
    assert result["uploaded_size"].at["row"] == 6
    assert dict(_ArchiveIncrement.__dict__) == original_archive_class

    control = LauncherRepository(storage).open(run.id)
    submission = control.read_submission()
    encoded_inputs = {item["name"]: item for item in submission["invocation"]["inputs"]}
    loaded = load_invocation(
        workflow,
        submission["invocation"],
        control_dir=control.control_dir,
    )
    assert loaded.inputs["table"].at["row", "dataframe_path"] == Path(
        "/cluster/data/from-frame.tif"
    )
    assert loaded.inputs["table"].at["row", "label"] == "/looks/local/but-is-a-string"
    installed_upload = Path(encoded_inputs["upload"]["value"]["value"])
    assert installed_upload.read_bytes() == b"pixels"
    assert encoded_inputs["cluster_path"]["value"] == {
        "tag": "path",
        "value": "/cluster/data/reference.tif",
    }
    assert encoded_inputs["note"]["value"] == {
        "tag": "str",
        "value": "/looks/local/but-is-a-string",
    }
    assert cluster.launch_count == 1
    assert not any(
        (staging / name).exists() for name in ("cache", "launcher", "outputs", "views")
    )


def test_remote_cluster_exposes_structured_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = tmp_path / "cluster-storage"
    staging = tmp_path / "transport"
    cluster = FakeCluster()
    cluster.install(monkeypatch)
    workflow = Workflow(storage_path=storage, engine="direct")
    with workflow:
        ParslFail()(value=7, name="first")
        ParslFail()(value=9, name="second")

    run = submit_workflow(
        workflow,
        targets=["second", "first"],
        parsl_config=_config(storage),
        executor_bindings={"threads": _binding()},
        shared_runtime_root=storage / "shared-runtime",
        launch=_launch(),
        transport=_transport(staging),
    )

    assert cluster.run_queued_job() == "failed"
    run.refresh()
    with pytest.raises(WorkflowRunFailedError, match="remote failure 7"):
        run.result(destination=tmp_path / "unused")
    statuses = [
        entry["payload"]["status"]
        for entry in run.progress()
        if entry["kind"] == "public"
    ]
    assert statuses[0] == "started"
    assert statuses[-1] == "failed"
