"""Conditional launcher control artifacts outside the guarded status file."""

from __future__ import annotations

import json
import os
import traceback as traceback_module
import uuid
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from .errors import LauncherProtocolError
from .repository import LauncherRunControl
from .schemas import (
    ERROR_SCHEMA,
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
    encoded = json.dumps(
        dict(payload),
        indent=2,
        sort_keys=True,
        allow_nan=False,
    ).encode("utf-8")
    if immutable:
        try:
            descriptor = os.open(
                path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
        except FileExistsError as exc:
            raise LauncherProtocolError(
                f"Immutable launcher artifact {relative!r} already exists."
            ) from exc
        try:
            os.write(descriptor, encoded)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        return path
    temporary = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    try:
        temporary.write_bytes(encoded)
        with temporary.open("rb") as handle:
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
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


def write_error(
    control: LauncherRunControl,
    *,
    code: str,
    error: BaseException,
    traceback_text: str | None = None,
    node: Mapping[str, Any] | None = None,
    task: Mapping[str, Any] | None = None,
    backend: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Persist one immutable structured terminal error without secrets."""
    payload = validate_error(
        {
            "schema": ERROR_SCHEMA,
            "run_id": control.run_id,
            "code": code,
            "exception_type": type(error).__name__,
            "message": str(error),
            "traceback": (
                traceback_text
                if traceback_text is not None
                else "".join(
                    traceback_module.format_exception(
                        type(error),
                        error,
                        error.__traceback__,
                    )
                )
            ),
            "node": None if node is None else dict(node),
            "task": None if task is None else dict(task),
            "backend": None if backend is None else dict(backend),
        }
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
