"""Shell-free OpenSSH and SFTP client for private cluster submission."""

from __future__ import annotations

import json
import math
import os
import subprocess
import uuid
from collections.abc import Mapping, Sequence
from pathlib import Path, PurePosixPath
from typing import Any, cast

from bioimageflow.parsl import ExecutorBinding, ParslTaskPolicy
from bioimageflow.storage import canonical_json_bytes
from bioimageflow.workflow import Workflow

from .cluster_bundle import PreparedClusterBundle, prepare_cluster_bundle
from .cluster_protocol import (
    MAX_RESPONSE_BYTES,
    RESPONSE_SCHEMA,
    request,
)
from .schemas import validate_run_id
from .types import PSIJLaunchConfig, ParslConfigRef, SSHSubmissionTransport


_ENV_ALLOWLIST = (
    "HOME",
    "LANG",
    "LC_ALL",
    "LOGNAME",
    "PATH",
    "SSH_AUTH_SOCK",
    "USER",
)


class SSHTransportError(RuntimeError):
    """Stable local transport failure with an actionable category."""

    def __init__(self, code: str, message: str, *, ambiguous: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.ambiguous = ambiguous


def _subprocess_environment() -> dict[str, str]:
    return {name: os.environ[name] for name in _ENV_ALLOWLIST if name in os.environ}


def _timeout_seconds(value: float) -> int:
    return max(1, math.ceil(value))


def _decode_response(encoded: bytes, request_id: str) -> dict[str, Any]:
    if len(encoded) > MAX_RESPONSE_BYTES:
        raise SSHTransportError(
            "remote-protocol",
            "Cluster response exceeds the protocol byte limit.",
        )
    try:
        value = json.loads(encoded.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SSHTransportError(
            "remote-protocol",
            "Cluster command returned malformed protocol JSON.",
            ambiguous=True,
        ) from exc
    if type(value) is not dict or set(value) != {
        "error",
        "ok",
        "request_id",
        "result",
        "schema",
    } or value["schema"] != RESPONSE_SCHEMA:
        raise SSHTransportError(
            "remote-protocol",
            "Cluster command response schema is invalid.",
            ambiguous=True,
        )
    if value["request_id"] != request_id:
        raise SSHTransportError(
            "remote-protocol",
            "Cluster response request ID does not match.",
            ambiguous=True,
        )
    if value["ok"] is True and value["error"] is None and type(value["result"]) is dict:
        return value["result"]
    error = value["error"]
    if (
        value["ok"] is not False
        or value["result"] is not None
        or type(error) is not dict
        or set(error) != {"code", "message", "retryable"}
    ):
        raise SSHTransportError(
            "remote-protocol",
            "Cluster error response schema is invalid.",
            ambiguous=True,
        )
    raise SSHTransportError(
        f"remote-{error['code']}",
        str(error["message"]),
        ambiguous=bool(error["retryable"]),
    )


def _ssh_failure_code(stderr: bytes) -> str:
    diagnostic = stderr.decode("utf-8", errors="replace").lower()
    if "host key verification failed" in diagnostic or "remote host identification" in diagnostic:
        return "ssh-host-key"
    if "permission denied" in diagnostic or "authentication failed" in diagnostic:
        return "ssh-authentication"
    if (
        "connection timed out" in diagnostic
        or "operation timed out" in diagnostic
        or "connect timeout" in diagnostic
    ):
        return "ssh-timeout"
    if (
        "could not resolve hostname" in diagnostic
        or "connection refused" in diagnostic
        or "no route to host" in diagnostic
        or "connection closed" in diagnostic
    ):
        return "ssh-connection"
    return "ssh-command-failed"


def execute_cluster_command(
    transport: SSHSubmissionTransport,
    operation: str,
    arguments: Mapping[str, Any],
    *,
    request_id: str,
) -> dict[str, Any]:
    """Execute one bounded cluster-agent request through system OpenSSH."""
    envelope = request(operation, arguments, request_id=request_id)
    encoded = canonical_json_bytes(envelope)
    argv = [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        f"ConnectTimeout={_timeout_seconds(transport.connect_timeout)}",
        "--",
        transport.host,
        str(transport.remote_executable),
    ]
    try:
        completed = subprocess.run(
            argv,
            input=encoded,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            shell=False,
            timeout=transport.connect_timeout,
            env=_subprocess_environment(),
        )
    except subprocess.TimeoutExpired as exc:
        raise SSHTransportError(
            "ssh-timeout",
            "OpenSSH command timed out.",
            ambiguous=True,
        ) from exc
    except OSError as exc:
        raise SSHTransportError(
            "ssh-unavailable",
            "System OpenSSH executable could not be started.",
        ) from exc
    if completed.returncode != 0:
        raise SSHTransportError(
            _ssh_failure_code(completed.stderr),
            "OpenSSH connection, authentication, host-key, or remote command failed.",
            ambiguous=True,
        )
    return _decode_response(completed.stdout, request_id)


def _sftp_quote(value: str) -> str:
    if any(character in value for character in ("\x00", "\n", "\r")):
        raise ValueError("SFTP paths must not contain controls or newlines.")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def upload_bundle(
    transport: SSHSubmissionTransport,
    bundle: PreparedClusterBundle,
    remote_root: str,
) -> None:
    """Upload bundle files only beneath one server-issued partial root."""
    remote = PurePosixPath(remote_root)
    staging = transport.staging_root
    try:
        remote.relative_to(staging / ".partial")
    except ValueError as exc:
        raise SSHTransportError(
            "unsafe-upload-target",
            "Server-issued upload path escapes transport staging.",
        ) from exc
    directories: set[PurePosixPath] = set()
    files = []
    for entry in bundle.manifest["entries"]:
        relative = PurePosixPath(entry["path"])
        parent = relative.parent
        while str(parent) != ".":
            directories.add(parent)
            parent = parent.parent
        if entry["kind"] == "directory":
            directories.add(relative)
        else:
            files.append((relative, bundle.root / Path(*relative.parts)))
    commands = []
    for directory in sorted(directories, key=lambda item: (len(item.parts), str(item))):
        commands.append(f"mkdir {_sftp_quote(str(remote / directory))}")
    for relative, local in files:
        commands.append(
            f"put {_sftp_quote(str(local))} {_sftp_quote(str(remote / relative))}"
        )
    batch = ("\n".join(commands) + "\n").encode("utf-8")
    argv = [
        "sftp",
        "-b",
        "-",
        "-o",
        "BatchMode=yes",
        "-o",
        f"ConnectTimeout={_timeout_seconds(transport.connect_timeout)}",
        "--",
        transport.host,
    ]
    try:
        completed = subprocess.run(
            argv,
            input=batch,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            shell=False,
            timeout=transport.connect_timeout,
            env=_subprocess_environment(),
        )
    except subprocess.TimeoutExpired as exc:
        raise SSHTransportError(
            "sftp-timeout",
            "SFTP upload timed out.",
        ) from exc
    except OSError as exc:
        raise SSHTransportError(
            "sftp-unavailable",
            "System SFTP executable could not be started.",
        ) from exc
    if completed.returncode != 0:
        raise SSHTransportError(
            "sftp-transfer-failed",
            "SFTP did not complete the allocated upload.",
        )


def _retry_mutation(
    transport: SSHSubmissionTransport,
    operation: str,
    arguments: Mapping[str, Any],
    request_id: str,
) -> dict[str, Any]:
    try:
        return execute_cluster_command(
            transport,
            operation,
            arguments,
            request_id=request_id,
        )
    except SSHTransportError as first:
        if not first.ambiguous:
            raise
        return execute_cluster_command(
            transport,
            operation,
            arguments,
            request_id=request_id,
        )


def submit_cluster_workflow(
    workflow: Workflow,
    *,
    transport: SSHSubmissionTransport,
    inputs: Mapping[str, Any] | None,
    targets: Sequence[str] | None,
    parsl_config: ParslConfigRef,
    executor_bindings: Mapping[str, ExecutorBinding],
    node_routes: Mapping[str, str] | None = None,
    environment_routes: Mapping[str, str] | None = None,
    shared_runtime_root: Path | str | None = None,
    task_policy: ParslTaskPolicy | None = None,
    launch: PSIJLaunchConfig,
) -> str:
    """Private WP2 seam returning one remotely allocated launcher run ID."""
    try:
        with prepare_cluster_bundle(
            workflow,
            inputs=inputs,
            targets=targets,
            parsl_config=parsl_config,
            executor_bindings=executor_bindings,
            node_routes=node_routes,
            environment_routes=environment_routes,
            shared_runtime_root=shared_runtime_root,
            task_policy=task_policy,
            launch=launch,
        ) as bundle:
            base = {
                "manifest": bundle.manifest,
                "staging_root": str(transport.staging_root),
            }
            allocated = _retry_mutation(
                transport,
                "allocate-upload",
                base,
                str(uuid.uuid4()),
            )
            upload_bundle(transport, bundle, allocated["remote_root"])
            committed = _retry_mutation(
                transport,
                "commit-upload",
                {**base, "upload_id": allocated["upload_id"]},
                str(uuid.uuid4()),
            )
            submitted = _retry_mutation(
                transport,
                "submit",
                {**base, "object_path": committed["object_path"]},
                str(uuid.uuid4()),
            )
            run_id = submitted.get("run_id")
            try:
                canonical = validate_run_id(run_id)
            except (TypeError, ValueError) as exc:
                raise SSHTransportError(
                    "remote-protocol",
                    "Cluster submit response contains an invalid run ID.",
                    ambiguous=True,
                ) from exc
            if canonical != run_id:
                raise SSHTransportError(
                    "remote-protocol",
                    "Cluster submit response contains an invalid run ID.",
                    ambiguous=True,
                )
            return cast(str, run_id)
    except SSHTransportError:
        raise
    except (OSError, TypeError, ValueError) as exc:
        raise SSHTransportError(
            "local-packaging",
            f"Local submission packaging failed: {type(exc).__name__}.",
        ) from exc
