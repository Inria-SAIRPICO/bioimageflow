"""Conditional launcher control artifacts outside the guarded status file."""

from __future__ import annotations

import json
import traceback as traceback_module
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from .errors import LauncherProtocolError
from .control import LauncherRunControl
from .repository import _atomic_create_json, _atomic_write_json
from .schemas import (
    ERROR_SCHEMA,
    parse_utc_timestamp,
    utc_timestamp,
    validate_error,
    validate_versioned_payload,
)


def _write_json(
    control: LauncherRunControl,
    relative: str,
    payload: Mapping[str, Any],
    *,
    immutable: bool,
) -> Path:
    path = control.confined_path(relative)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.parent.is_symlink():
        raise LauncherProtocolError(
            f"Launcher artifact parent for {relative!r} must not be a symlink."
        )
    if immutable:
        try:
            _atomic_create_json(path, payload)
        except FileExistsError as exc:
            existing = read_json(control, relative)
            if existing != dict(payload):
                raise LauncherProtocolError(
                    f"Immutable launcher artifact {relative!r} already exists "
                    "with different content."
                ) from exc
        return path
    _atomic_write_json(path, payload)
    return path


def read_json(
    control: LauncherRunControl,
    relative: str,
) -> dict[str, Any]:
    """Read one confined, non-symlink JSON artifact."""
    path = control.confined_path(relative, must_exist=True)
    if path.is_symlink() or not path.is_file():
        raise LauncherProtocolError(
            f"Launcher artifact {relative!r} must be a regular file."
        )
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise LauncherProtocolError(
            f"Launcher artifact {relative!r} is invalid JSON."
        ) from exc
    if not isinstance(value, dict):
        raise LauncherProtocolError(
            f"Launcher artifact {relative!r} must be a JSON object."
        )
    return value


def build_error_payload(
    run_id: str,
    *,
    code: str,
    error: BaseException,
    traceback_text: str | None = None,
    node: Mapping[str, Any] | None = None,
    task: Mapping[str, Any] | None = None,
    backend: Mapping[str, Any] | None = None,
    redactions: Sequence[str] = (),
) -> dict[str, Any]:
    """Build one validated structured terminal error without literal secrets."""
    message = str(error)
    formatted_traceback = (
        traceback_text
        if traceback_text is not None
        else "".join(
            traceback_module.format_exception(
                type(error),
                error,
                error.__traceback__,
            )
        )
    )
    for secret in redactions:
        if type(secret) is str and secret:
            message = message.replace(secret, "[REDACTED]")
            formatted_traceback = formatted_traceback.replace(
                secret,
                "[REDACTED]",
            )
    return validate_error(
        {
            "schema": ERROR_SCHEMA,
            "run_id": run_id,
            "code": code,
            "exception_type": type(error).__name__,
            "message": message,
            "traceback": formatted_traceback,
            "node": None if node is None else dict(node),
            "task": None if task is None else dict(task),
            "backend": None if backend is None else dict(backend),
        }
    )


def write_error(
    control: LauncherRunControl,
    *,
    code: str,
    error: BaseException,
    traceback_text: str | None = None,
    node: Mapping[str, Any] | None = None,
    task: Mapping[str, Any] | None = None,
    backend: Mapping[str, Any] | None = None,
    redactions: Sequence[str] = (),
) -> dict[str, Any]:
    """Persist one immutable structured terminal error without secrets."""
    payload = build_error_payload(
        control.run_id,
        code=code,
        error=error,
        traceback_text=traceback_text,
        node=node,
        task=task,
        backend=backend,
        redactions=redactions,
    )
    _write_json(control, "error.json", payload, immutable=True)
    return payload


def read_error(control: LauncherRunControl) -> dict[str, Any]:
    """Load and validate the persisted terminal error."""
    try:
        payload = validate_versioned_payload(read_json(control, "error.json"))
    except Exception as exc:
        raise LauncherProtocolError("Launcher terminal error is unavailable.") from exc
    if payload.get("schema") != ERROR_SCHEMA or payload.get("run_id") != control.run_id:
        raise LauncherProtocolError("Launcher terminal error correlation mismatch.")
    return payload


def write_manual_command(
    control: LauncherRunControl,
    *,
    argv: Sequence[str],
    work_dir: Path | None,
    secret_refs: Sequence[str],
) -> dict[str, Any]:
    """Persist a structured, shell-free manual orchestrator command."""
    if (
        not argv
        or any(type(argument) is not str or not argument for argument in argv)
        or any(type(reference) is not str or not reference for reference in secret_refs)
    ):
        raise ValueError("Manual command arguments and secret refs must be strings.")
    payload = {
        "schema": "bioimageflow.launcher.command.v1",
        "run_id": control.run_id,
        "argv": list(argv),
        "work_dir": None if work_dir is None else str(work_dir),
        "secret_refs": list(secret_refs),
    }
    _write_json(control, "command.json", payload, immutable=True)
    return payload


def read_manual_command(control: LauncherRunControl) -> dict[str, Any]:
    """Load the structured manual command descriptor."""
    payload = read_json(control, "command.json")
    if set(payload) != {"schema", "run_id", "argv", "work_dir", "secret_refs"}:
        raise LauncherProtocolError("Manual command descriptor fields are invalid.")
    if (
        payload["schema"] != "bioimageflow.launcher.command.v1"
        or payload["run_id"] != control.run_id
        or not isinstance(payload["argv"], list)
        or not payload["argv"]
        or not all(type(item) is str and item for item in payload["argv"])
        or not isinstance(payload["secret_refs"], list)
        or not all(type(item) is str and item for item in payload["secret_refs"])
    ):
        raise LauncherProtocolError("Manual command descriptor is invalid.")
    work_dir = payload["work_dir"]
    if work_dir is not None and (
        type(work_dir) is not str or not Path(work_dir).is_absolute()
    ):
        raise LauncherProtocolError(
            "Manual command work_dir must be absolute or null."
        )
    return payload


def write_local_process_identity(
    control: LauncherRunControl,
    *,
    pid: int,
    start_token: str,
) -> dict[str, Any]:
    """Persist the reconnectable identity of one local orchestrator."""
    if (
        type(pid) is not int
        or pid <= 0
        or type(start_token) is not str
        or not start_token
    ):
        raise ValueError("Local process identity is invalid.")
    payload = {
        "schema": "bioimageflow.launcher.local_process.v1",
        "run_id": control.run_id,
        "pid": pid,
        "start_token": start_token,
        "started_at": utc_timestamp(),
    }
    _write_json(control, "local_process.json", payload, immutable=True)
    return payload


def read_local_process_identity(
    control: LauncherRunControl,
) -> dict[str, Any]:
    """Read and validate one persisted local orchestrator identity."""
    payload = read_json(control, "local_process.json")
    if set(payload) != {
        "schema",
        "run_id",
        "pid",
        "start_token",
        "started_at",
    }:
        raise LauncherProtocolError("Local process identity fields are invalid.")
    if (
        payload["schema"] != "bioimageflow.launcher.local_process.v1"
        or payload["run_id"] != control.run_id
        or type(payload["pid"]) is not int
        or payload["pid"] <= 0
        or type(payload["start_token"]) is not str
        or not payload["start_token"]
        or type(payload["started_at"]) is not str
        or not payload["started_at"]
    ):
        raise LauncherProtocolError("Local process identity is invalid.")
    try:
        parse_utc_timestamp(
            payload["started_at"],
            field="local_process.started_at",
        )
    except Exception as exc:
        raise LauncherProtocolError(
            "Local process identity timestamp is invalid."
        ) from exc
    return payload


def write_local_process_exit(
    control: LauncherRunControl,
    *,
    returncode: int,
) -> dict[str, Any]:
    """Persist a complete local child exit observation."""
    if type(returncode) is not int:
        raise TypeError("Local process returncode must be an integer.")
    payload = {
        "schema": "bioimageflow.launcher.local_process_exit.v1",
        "run_id": control.run_id,
        "returncode": returncode,
        "observed_at": utc_timestamp(),
    }
    _write_json(control, "local_process_exit.json", payload, immutable=True)
    return payload
