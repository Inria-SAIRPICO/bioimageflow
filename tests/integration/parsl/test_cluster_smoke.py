"""Optional real-site PSI/J smoke configured only by a maintainer-owned file."""

from __future__ import annotations

import json
import math
import os
from datetime import timedelta
from importlib.metadata import version
from pathlib import Path, PurePosixPath
from typing import Any

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


def _reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            pytest.fail("The PSI/J smoke configuration contains a duplicate key.")
        value[key] = item
    return value


def _reject_nonfinite(value: str) -> None:
    pytest.fail(f"The PSI/J smoke configuration contains non-finite JSON {value!r}.")


def _site_config() -> dict[str, Any]:
    path_value = os.environ.get("BIOIMAGEFLOW_PSIJ_SMOKE_CONFIG")
    if path_value is None:
        pytest.skip("BIOIMAGEFLOW_PSIJ_SMOKE_CONFIG is not set.")
    path = Path(path_value)
    if not path.is_absolute() or not path.is_file():
        pytest.fail("BIOIMAGEFLOW_PSIJ_SMOKE_CONFIG must name an absolute JSON file.")
    try:
        value = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_reject_duplicates,
            parse_constant=_reject_nonfinite,
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        pytest.fail(f"The PSI/J smoke configuration is unreadable: {error}.")
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
        "timeout_seconds",
        "walltime_seconds",
    }
    optional = {"cpu_cores", "project", "queue"}
    if (
        type(value) is not dict
        or set(value) != required | (set(value) & optional)
        or not required.issubset(value)
    ):
        pytest.fail("The PSI/J smoke configuration has missing or unknown fields.")
    if value["executor"] not in {"slurm", "pbs", "lsf"}:
        pytest.fail("The PSI/J smoke executor must be slurm, pbs, or lsf.")
    for field in (
        "host",
        "parsl_config_factory",
        "remote_executable",
        "shared_runtime_root",
        "staging_root",
        "storage_path",
    ):
        if (
            type(value[field]) is not str
            or not value[field]
            or value[field] != value[field].strip()
        ):
            pytest.fail(f"The PSI/J smoke field {field!r} must be a non-empty string.")
    for field in (
        "remote_executable",
        "shared_runtime_root",
        "staging_root",
        "storage_path",
    ):
        encoded = value[field]
        path_value = PurePosixPath(encoded)
        if (
            not path_value.is_absolute()
            or encoded.startswith("//")
            or str(path_value) != encoded
        ):
            pytest.fail(
                f"The PSI/J smoke field {field!r} must be a normalized absolute path."
            )
    if type(value["parsl_config_kwargs"]) is not dict:
        pytest.fail("The PSI/J smoke parsl_config_kwargs field must be an object.")
    bindings = value["executor_bindings"]
    if (
        type(bindings) is not dict
        or not bindings
        or any(
            type(name) is not str or not name or type(binding) is not dict
            for name, binding in bindings.items()
        )
    ):
        pytest.fail(
            "The PSI/J smoke executor_bindings field must be a non-empty object."
        )
    for field in ("walltime_seconds", "timeout_seconds"):
        duration = value[field]
        if (
            type(duration) not in {int, float}
            or not math.isfinite(float(duration))
            or float(duration) <= 0
        ):
            pytest.fail(f"The PSI/J smoke field {field!r} must be finite and positive.")
    if "cpu_cores" in value and (
        type(value["cpu_cores"]) is not int or value["cpu_cores"] <= 0
    ):
        pytest.fail("The PSI/J smoke cpu_cores field must be a positive integer.")
    for field in ("project", "queue"):
        if field in value and (
            type(value[field]) is not str
            or not value[field]
            or value[field] != value[field].strip()
        ):
            pytest.fail(f"The PSI/J smoke field {field!r} must be a non-empty string.")
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
    assert (
        run.wait(
            timeout=float(config["timeout_seconds"]),
            poll_interval=2.0,
        )
        == "succeeded"
    )
    reopened = RemoteWorkflowRun.open(
        transport, workflow.storage_path.as_posix(), run.id
    )
    native_ids = {
        event["payload"]["native_id"]
        for event in reopened.progress()
        if event["kind"] == "backend" and event["payload"].get("native_id") is not None
    }
    assert len(native_ids) == 1
    destination = tmp_path / "result"
    result = reopened.result(destination=destination)
    assert result.at["0", "size"] == len(b"cluster-smoke")
