"""Optional real-site PSI/J smoke configured only by a maintainer-owned file."""

from __future__ import annotations

import json
import os
from importlib.metadata import version
from datetime import timedelta
from pathlib import Path, PurePosixPath

import pytest

from bioimageflow import (
    ExecutorBinding,
    LocalUpload,
    PSIJLaunchConfig,
    ParslConfigRef,
    RemoteWorkflowRun,
    SSHSubmissionTransport,
    Workflow,
    submit_workflow,
)
from bioimageflow_core import Arguments, EnvironmentSpec, IOModel, ProcessingTool

pytestmark = [pytest.mark.parsl, pytest.mark.slow, pytest.mark.cluster_smoke]


class _ReadUploadedFile(ProcessingTool):
    environment = EnvironmentSpec(
        name="cluster-smoke",
        dependencies={
            "python": "3.10",
            "pip": [f"bioimageflow-core=={version('bioimageflow-core')}"],
        },
    )

    class Inputs(IOModel):
        path: Path

    class Outputs(IOModel):
        size: int

    def process_row(self, arguments: Arguments) -> "_ReadUploadedFile.Outputs":
        return self.Outputs(size=arguments.path.stat().st_size)


def _site_config() -> dict:
    path_value = os.environ.get("BIOIMAGEFLOW_PSIJ_SMOKE_CONFIG")
    if path_value is None:
        pytest.skip("BIOIMAGEFLOW_PSIJ_SMOKE_CONFIG is not set.")
    path = Path(path_value)
    if not path.is_absolute() or not path.is_file():
        pytest.fail("BIOIMAGEFLOW_PSIJ_SMOKE_CONFIG must name an absolute JSON file.")
    value = json.loads(path.read_text(encoding="utf-8"))
    required = {
        "executor_bindings",
        "executor",
        "host",
        "parsl_config_factory",
        "parsl_config_kwargs",
        "remote_executable",
        "shared_runtime_root",
        "staging_root",
        "storage_path",
        "walltime_seconds",
    }
    optional = {"cpu_cores", "project", "queue"}
    if type(value) is not dict or not required.issubset(value) or set(value) - required - optional:
        pytest.fail("The PSI/J smoke configuration has missing or unknown fields.")
    return value


def test_configured_real_psij_site_round_trip(tmp_path: Path) -> None:
    config = _site_config()
    transport = SSHSubmissionTransport(
        host=config["host"],
        staging_root=PurePosixPath(config["staging_root"]),
        remote_executable=PurePosixPath(config["remote_executable"]),
    )
    workflow = Workflow(storage_path=Path(config["storage_path"]), engine="direct")
    with workflow:
        upload = workflow.input("upload", Path, id="upload")
        read = _ReadUploadedFile()(path=upload, name="read-upload")
        workflow.output("size", read["size"], id="size")
    source = tmp_path / "payload.bin"
    source.write_bytes(b"cluster-smoke")

    run = submit_workflow(
        workflow,
        inputs={"upload": LocalUpload(source)},
        parsl_config=ParslConfigRef(
            config["parsl_config_factory"],
            config["parsl_config_kwargs"],
        ),
        executor_bindings={
            name: ExecutorBinding.from_dict(binding)
            for name, binding in config["executor_bindings"].items()
        },
        shared_runtime_root=config["shared_runtime_root"],
        launch=PSIJLaunchConfig(
            executor=config["executor"],
            walltime=timedelta(seconds=config["walltime_seconds"]),
            queue=config.get("queue"),
            project=config.get("project"),
            cpu_cores=config.get("cpu_cores", 1),
        ),
        transport=transport,
    )
    assert run.wait(poll_interval=2.0) == "succeeded"
    reopened = RemoteWorkflowRun.open(transport, workflow.storage_path.as_posix(), run.id)
    native_ids = {
        event["payload"]["native_id"]
        for event in reopened.progress()
        if event["kind"] == "backend"
        and event["payload"].get("native_id") is not None
    }
    assert len(native_ids) == 1
    destination = tmp_path / "result"
    result = reopened.result(destination=destination)
    assert result.at["0", "size"] == len(b"cluster-smoke")
