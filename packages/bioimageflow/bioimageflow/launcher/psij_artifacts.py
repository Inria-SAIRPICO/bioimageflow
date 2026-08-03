"""Strict immutable PSI/J launcher intent and receipt artifacts."""

from __future__ import annotations

import hashlib
import os
import re
import stat
import uuid
from pathlib import Path
from typing import Any

from .artifacts import _write_json
from .backends import build_orchestrator_argv, launcher_log_paths
from .control import LauncherRunControl
from .errors import LauncherProtocolError
from .repository import LauncherCorruptionError, _atomic_create_json, _read_json
from .schemas import PSIJ_NATIVE_ID_PATTERN, parse_utc_timestamp, utc_timestamp
from .types import PSIJLaunchConfig


INTENT_SCHEMA = "bioimageflow.launcher.psij_intent.v2"
RECEIPT_SCHEMA = "bioimageflow.launcher.psij_job.v1"


def _validated_pre_launch(
    control: LauncherRunControl,
) -> dict[str, Any] | None:
    metadata = control.read_submission()["psij_pre_launch"]
    if metadata is None:
        return None
    artifact = metadata["artifact"]
    path = control.confined_path(artifact["path"], must_exist=True)
    mode = path.lstat().st_mode
    if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
        raise LauncherProtocolError(
            "The PSI/J pre-launch artifact must be a regular non-symlink file."
        )
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    digest = hashlib.sha256()
    size = 0
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise LauncherProtocolError(
                "The PSI/J pre-launch artifact must be a regular file."
            )
        while True:
            chunk = os.read(descriptor, 64 * 1024)
            if not chunk:
                break
            size += len(chunk)
            if size > 64 * 1024:
                raise LauncherProtocolError(
                    "The PSI/J pre-launch artifact exceeds its size limit."
                )
            digest.update(chunk)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    final = path.lstat()
    identities = {
        (
            value.st_dev,
            value.st_ino,
            value.st_mode,
            value.st_size,
            value.st_mtime_ns,
        )
        for value in (opened, after, final)
    }
    if len(identities) != 1:
        raise LauncherProtocolError(
            "The PSI/J pre-launch artifact changed during validation."
        )
    observed = f"sha256:{digest.hexdigest()}"
    if size != artifact["size"] or observed != artifact["digest"]:
        raise LauncherProtocolError(
            "The PSI/J pre-launch artifact does not match its submission metadata."
        )
    return {"path": str(path), "size": size, "digest": observed}


def _read_artifact(
    control: LauncherRunControl,
    relative: str,
) -> dict[str, Any]:
    path = control.confined_path(relative, must_exist=True)
    try:
        return _read_json(path)
    except (OSError, LauncherCorruptionError) as error:
        raise LauncherProtocolError(
            f"PSI/J launcher artifact {relative!r} is invalid."
        ) from error


def _job_description(
    control: LauncherRunControl,
    launch: PSIJLaunchConfig,
    *,
    submit_token: str,
) -> dict[str, Any]:
    argv = build_orchestrator_argv(control)
    executable = Path(argv[0])
    if not executable.is_absolute():
        raise LauncherProtocolError(
            "The PSI/J orchestrator executable must be absolute."
        )
    stdout_path, stderr_path = launcher_log_paths(control)
    directory = None if launch.work_dir is None else str(launch.work_dir)
    pre_launch = _validated_pre_launch(control)
    return {
        "name": f"bioimageflow-{control.run_id}-{submit_token[:8]}",
        "executable": str(executable),
        "arguments": list(argv[1:]),
        "directory": directory,
        "stdout_path": str(stdout_path),
        "stderr_path": str(stderr_path),
        "pre_launch": pre_launch,
        "resources": {
            "node_count": 1,
            "process_count": 1,
            "processes_per_node": 1,
            "cpu_cores_per_process": launch.cpu_cores,
        },
        "attributes": {
            "duration_seconds": launch.walltime.total_seconds(),
            "queue_name": launch.queue,
            "account": launch.project,
        },
    }


def _validate_absolute(value: object, *, field_name: str) -> str:
    if type(value) is not str or not value or not Path(value).is_absolute():
        raise LauncherProtocolError(f"{field_name} must be an absolute path.")
    if Path(value).as_posix() != value or ".." in Path(value).parts:
        raise LauncherProtocolError(f"{field_name} must be normalized.")
    return value


def validate_intent(
    control: LauncherRunControl,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Validate the exact immutable submit-intent schema and correlation."""
    fields = {
        "schema",
        "run_id",
        "submit_token",
        "created_at",
        "executor",
        "executor_work_dir",
        "job",
    }
    if set(payload) != fields:
        raise LauncherProtocolError("PSI/J submit intent fields are invalid.")
    if (
        payload["schema"] != INTENT_SCHEMA
        or payload["run_id"] != control.run_id
        or payload["executor"] not in {"slurm", "pbs", "lsf"}
        or type(payload["submit_token"]) is not str
        or re.fullmatch(r"[0-9a-f]{32}", payload["submit_token"]) is None
    ):
        raise LauncherProtocolError("PSI/J submit intent identity is invalid.")
    parse_utc_timestamp(payload["created_at"], field="created_at")
    executor_work_dir = _validate_absolute(
        payload["executor_work_dir"],
        field_name="executor_work_dir",
    )
    if executor_work_dir != str(control.confined_path("psij/executor")):
        raise LauncherProtocolError(
            "PSI/J executor work directory is not the run's fixed confined path."
        )
    job = payload["job"]
    if not isinstance(job, dict) or set(job) != {
        "name",
        "executable",
        "arguments",
        "directory",
        "stdout_path",
        "stderr_path",
        "pre_launch",
        "resources",
        "attributes",
    }:
        raise LauncherProtocolError("PSI/J submit intent job fields are invalid.")
    if (
        job["name"] != f"bioimageflow-{control.run_id}-{payload['submit_token'][:8]}"
        or not isinstance(job["arguments"], list)
        or any(type(item) is not str or not item for item in job["arguments"])
    ):
        raise LauncherProtocolError("PSI/J submit intent command is invalid.")
    for field_name in ("executable", "stdout_path", "stderr_path"):
        _validate_absolute(job[field_name], field_name=field_name)
    argv = build_orchestrator_argv(control)
    stdout_path = control.confined_path("logs/orchestrator.out")
    stderr_path = control.confined_path("logs/orchestrator.err")
    if job["executable"] != argv[0] or job["arguments"] != list(argv[1:]):
        raise LauncherProtocolError(
            "PSI/J submit intent does not contain the exact orchestrator command."
        )
    if job["stdout_path"] != str(stdout_path) or job["stderr_path"] != str(stderr_path):
        raise LauncherProtocolError(
            "PSI/J submit intent logs are not the confined launcher logs."
        )
    if job["directory"] is not None:
        _validate_absolute(job["directory"], field_name="directory")
    if job["pre_launch"] != _validated_pre_launch(control):
        raise LauncherProtocolError(
            "PSI/J submit intent pre-launch metadata is invalid."
        )
    resources = job["resources"]
    if not isinstance(resources, dict) or set(resources) != {
        "node_count",
        "process_count",
        "processes_per_node",
        "cpu_cores_per_process",
    }:
        raise LauncherProtocolError("PSI/J resource intent is invalid.")
    if (
        resources["node_count"] != 1
        or resources["process_count"] != 1
        or resources["processes_per_node"] != 1
        or type(resources["cpu_cores_per_process"]) is not int
        or resources["cpu_cores_per_process"] <= 0
    ):
        raise LauncherProtocolError("PSI/J resource intent is invalid.")
    attributes = job["attributes"]
    if not isinstance(attributes, dict) or set(attributes) != {
        "duration_seconds",
        "queue_name",
        "account",
    }:
        raise LauncherProtocolError("PSI/J attribute intent is invalid.")
    persisted_launch = PSIJLaunchConfig.from_dict(control.read_submission()["launch"])
    described_launch = PSIJLaunchConfig(
        executor=payload["executor"],
        walltime=persisted_launch.walltime,
        queue=attributes["queue_name"],
        project=attributes["account"],
        cpu_cores=resources["cpu_cores_per_process"],
        work_dir=job["directory"],
        hard_cancel_after=persisted_launch.hard_cancel_after,
    )
    if (
        attributes["duration_seconds"] != persisted_launch.walltime.total_seconds()
        or described_launch != persisted_launch
    ):
        raise LauncherProtocolError(
            "PSI/J submit intent does not match the immutable launch config."
        )
    return payload


def read_intent(control: LauncherRunControl) -> dict[str, Any]:
    """Read and validate one immutable PSI/J submit intent."""
    return validate_intent(control, _read_artifact(control, "psij_intent.json"))


def install_intent(
    control: LauncherRunControl,
    launch: PSIJLaunchConfig,
    work_dir: Path,
) -> tuple[dict[str, Any], bool]:
    """Atomically elect the only caller allowed to submit the PSI/J job."""
    submit_token = uuid.uuid4().hex
    payload = validate_intent(
        control,
        {
            "schema": INTENT_SCHEMA,
            "run_id": control.run_id,
            "submit_token": submit_token,
            "created_at": utc_timestamp(),
            "executor": launch.executor,
            "executor_work_dir": str(work_dir),
            "job": _job_description(
                control,
                launch,
                submit_token=submit_token,
            ),
        },
    )
    path = control.confined_path("psij_intent.json")
    try:
        _atomic_create_json(path, payload)
    except FileExistsError:
        return read_intent(control), False
    return payload, True


def validate_native_id(value: object) -> str:
    """Validate a persisted scheduler-native PSI/J job identity."""
    if type(value) is not str or PSIJ_NATIVE_ID_PATTERN.fullmatch(value) is None:
        raise LauncherProtocolError("PSI/J returned an invalid native job ID.")
    return value


def validate_receipt(
    control: LauncherRunControl,
    intent: dict[str, Any],
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Validate one exact receipt against its immutable intent."""
    if set(payload) != {
        "schema",
        "run_id",
        "submit_token",
        "executor",
        "native_id",
        "created_at",
        "executor_work_dir",
    }:
        raise LauncherProtocolError("PSI/J job receipt fields are invalid.")
    if (
        payload["schema"] != RECEIPT_SCHEMA
        or payload["run_id"] != control.run_id
        or payload["submit_token"] != intent["submit_token"]
        or payload["executor"] != intent["executor"]
        or payload["executor_work_dir"] != intent["executor_work_dir"]
    ):
        raise LauncherProtocolError("PSI/J job receipt correlation is invalid.")
    validate_native_id(payload["native_id"])
    created = parse_utc_timestamp(payload["created_at"], field="created_at")
    intent_created = parse_utc_timestamp(intent["created_at"], field="created_at")
    if created < intent_created:
        raise LauncherProtocolError("PSI/J job receipt predates its submit intent.")
    return payload


def read_receipt(
    control: LauncherRunControl,
    intent: dict[str, Any],
) -> dict[str, Any]:
    """Read and correlate one immutable native-job receipt."""
    return validate_receipt(
        control,
        intent,
        _read_artifact(control, "psij_job.json"),
    )


def write_receipt(
    control: LauncherRunControl,
    intent: dict[str, Any],
    *,
    native_id: str,
) -> dict[str, Any]:
    """Install a native-job receipt immediately after successful submit."""
    payload = validate_receipt(
        control,
        intent,
        {
            "schema": RECEIPT_SCHEMA,
            "run_id": control.run_id,
            "submit_token": intent["submit_token"],
            "executor": intent["executor"],
            "native_id": native_id,
            "created_at": utc_timestamp(),
            "executor_work_dir": intent["executor_work_dir"],
        },
    )
    _write_json(control, "psij_job.json", payload, immutable=True)
    return payload
