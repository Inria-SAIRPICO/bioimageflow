"""Shell-free OpenSSH and SFTP client for private cluster submission."""

from __future__ import annotations

import json
import base64
import binascii
import math
import os
import re
import subprocess
import uuid
from collections.abc import Mapping, Sequence
from pathlib import Path, PurePosixPath
from typing import Any

from bioimageflow.parsl import ExecutorBinding, ParslTaskPolicy
from bioimageflow.storage import canonical_json_bytes
from bioimageflow.workflow import Workflow

from .cluster_bundle import PreparedClusterBundle, prepare_cluster_bundle
from .cluster_protocol import (
    MAX_RESPONSE_BYTES,
    RESPONSE_SCHEMA,
    ClusterProtocolFailure,
    _check_tree,
    request,
)
from .schemas import validate_run_id
from .pre_launch import PreLaunchScript
from .types import PSIJLaunchConfig, ParslConfigRef, SSHSubmissionTransport


_ENV_ALLOWLIST = ("HOME", "LANG", "LC_ALL", "LOGNAME", "PATH", "SSH_AUTH_SOCK", "USER")


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


def _reject_response_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise SSHTransportError(
                "remote-protocol",
                "Cluster response contains a duplicate JSON key.",
                ambiguous=True,
            )
        result[key] = value
    return result


def _reject_response_nonfinite(value: str) -> None:
    raise SSHTransportError(
        "remote-protocol",
        "Cluster response contains a non-finite JSON number.",
        ambiguous=True,
    )


def _canonical_uuid4(value: Any, *, field: str) -> str:
    try:
        canonical = str(uuid.UUID(value, version=4))
    except (AttributeError, TypeError, ValueError) as exc:
        raise SSHTransportError(
            "remote-protocol",
            f"Cluster response contains an invalid {field}.",
            ambiguous=True,
        ) from exc
    if canonical != value:
        raise SSHTransportError(
            "remote-protocol",
            f"Cluster response contains an invalid {field}.",
            ambiguous=True,
        )
    return canonical


_OBSERVATION_FIELDS = {
    "error",
    "run_id",
    "state",
    "status_revision",
    "storage_path",
    "submission_schema",
    "status_schema",
    "terminal",
    "updated_at",
}
_RUN_STATES = {
    "prepared",
    "starting",
    "running",
    "cancel_requested",
    "finalizing",
    "succeeded",
    "failed",
    "cancelled",
    "lost",
}
_OBSERVATION_ERROR_FIELDS = {"code", "exception_type", "message", "run_id"}


def _validate_observation(
    result: Mapping[str, Any],
    arguments: Mapping[str, Any],
) -> None:
    if (
        not _OBSERVATION_FIELDS.issubset(result)
        or result["run_id"] != arguments.get("run_id")
        or result["storage_path"] != arguments.get("storage_path")
        or result["state"] not in _RUN_STATES
        or type(result["status_revision"]) is not int
        or result["status_revision"] < 0
        or type(result["terminal"]) is not bool
        or result["terminal"]
        != (result["state"] in {"succeeded", "failed", "cancelled", "lost"})
        or result["submission_schema"] != "bioimageflow.launcher.submission.v2"
        or result["status_schema"] != "bioimageflow.launcher.status.v1"
        or type(result["updated_at"]) is not str
        or not result["updated_at"]
        or (
            result["error"] is not None
            and (
                type(result["error"]) is not dict
                or set(result["error"]) != _OBSERVATION_ERROR_FIELDS
                or result["error"]["run_id"] != result["run_id"]
                or type(result["error"]["code"]) is not str
                or not result["error"]["code"]
                or type(result["error"]["run_id"]) is not str
                or not result["error"]["run_id"]
                or any(
                    result["error"][field] is not None
                    and type(result["error"][field]) is not str
                    for field in ("exception_type", "message")
                )
            )
        )
    ):
        raise SSHTransportError(
            "remote-protocol",
            "Cluster observation response is invalid.",
            ambiguous=True,
        )


def _validate_progress_result(
    result: Mapping[str, Any],
    arguments: Mapping[str, Any],
) -> None:
    events = result["events"]
    after = arguments.get("after_sequence")
    limit = arguments.get("limit")
    if (
        type(events) is not list
        or type(limit) is not int
        or len(events) > limit
        or type(result["has_more"]) is not bool
        or type(result["next_sequence"]) is not int
    ):
        raise SSHTransportError(
            "remote-protocol",
            "Cluster progress page is invalid.",
            ambiguous=True,
        )
    previous = after
    for event in events:
        if (
            type(event) is not dict
            or type(event.get("sequence")) is not int
            or event["sequence"] <= previous
        ):
            raise SSHTransportError(
                "remote-protocol",
                "Cluster progress sequence is invalid.",
                ambiguous=True,
            )
        previous = event["sequence"]
    if result["next_sequence"] != previous:
        raise SSHTransportError(
            "remote-protocol",
            "Cluster progress cursor is invalid.",
            ambiguous=True,
        )


def _validate_log_result(
    result: Mapping[str, Any],
    arguments: Mapping[str, Any],
) -> None:
    if (
        result["stream"] != arguments.get("stream")
        or type(result["exists"]) is not bool
        or type(result["eof"]) is not bool
        or type(result["reset"]) is not bool
        or type(result["next_offset"]) is not int
        or result["next_offset"] < 0
        or type(result["snapshot_size"]) is not int
        or result["snapshot_size"] < 0
        or (
            result["identity"] is not None
            and (type(result["identity"]) is not str or not result["identity"])
        )
        or type(result["data"]) is not str
        or (result["exists"] and result["identity"] is None)
        or (
            not result["exists"]
            and (
                result["identity"],
                result["data"],
                result["eof"],
                result["next_offset"],
                result["snapshot_size"],
            )
            != (None, "", True, 0, 0)
        )
    ):
        raise SSHTransportError(
            "remote-protocol",
            "Cluster log page is invalid.",
            ambiguous=True,
        )
    try:
        decoded = base64.b64decode(result["data"], validate=True)
    except (ValueError, binascii.Error) as exc:
        raise SSHTransportError(
            "remote-protocol",
            "Cluster log page contains invalid base64.",
            ambiguous=True,
        ) from exc
    requested_offset = arguments.get("offset")
    if type(requested_offset) is not int:
        raise SSHTransportError(
            "remote-protocol",
            "Cluster log request offset is invalid.",
            ambiguous=True,
        )
    expected_offset = 0 if result["reset"] else requested_offset
    if (
        result["next_offset"] != expected_offset + len(decoded)
        or result["next_offset"] > result["snapshot_size"]
        or result["eof"] != (result["next_offset"] >= result["snapshot_size"])
        or (
            not result["reset"]
            and arguments.get("snapshot_size") is not None
            and result["snapshot_size"] != arguments["snapshot_size"]
        )
        or (
            not result["eof"]
            and not decoded
        )
    ):
        raise SSHTransportError(
            "remote-protocol",
            "Cluster log page offset is inconsistent.",
            ambiguous=True,
        )


def _validate_success_result(
    operation: str,
    result: dict[str, Any],
    transport: SSHSubmissionTransport,
    arguments: Mapping[str, Any],
) -> dict[str, Any]:
    if operation == "validate-profile":
        from .profile_validation import RemoteProfileValidationReport

        try:
            RemoteProfileValidationReport.from_dict(result)
        except (TypeError, ValueError) as exc:
            raise SSHTransportError(
                "remote-protocol",
                "Cluster profile validation response is invalid.",
                ambiguous=True,
            ) from exc
        return result
    if operation == "allocate-upload":
        if set(result) != {"remote_root", "upload_id"}:
            raise SSHTransportError(
                "remote-protocol",
                "Cluster allocate response schema is invalid.",
                ambiguous=True,
            )
        upload_id = _canonical_uuid4(result["upload_id"], field="upload ID")
        expected = transport.staging_root / ".partial" / upload_id
        if result["remote_root"] != str(expected):
            raise SSHTransportError(
                "remote-protocol",
                "Cluster allocate response contains an unsafe upload path.",
                ambiguous=True,
            )
        return result
    if operation in {
        "inspect",
        "refresh",
        "read-progress",
        "read-logs",
        "cancel",
    }:
        _validate_observation(result, arguments)
        if operation in {"inspect", "refresh", "cancel"}:
            if set(result) != _OBSERVATION_FIELDS:
                raise SSHTransportError(
                    "remote-protocol",
                    "Cluster observation response schema is invalid.",
                    ambiguous=True,
                )
            return result
        if operation == "read-progress":
            expected = _OBSERVATION_FIELDS | {
                "events",
                "has_more",
                "next_sequence",
            }
            if set(result) != expected:
                raise SSHTransportError(
                    "remote-protocol",
                    "Cluster progress response schema is invalid.",
                    ambiguous=True,
                )
            _validate_progress_result(result, arguments)
            return result
        expected = _OBSERVATION_FIELDS | {
            "data",
            "eof",
            "exists",
            "identity",
            "next_offset",
            "reset",
            "snapshot_size",
            "stream",
        }
        if set(result) != expected:
            raise SSHTransportError(
                "remote-protocol",
                "Cluster log response schema is invalid.",
                ambiguous=True,
            )
        _validate_log_result(result, arguments)
        return result
    if operation == "prepare-result":
        if set(result) != {
            "bundle_digest",
            "remote_root",
            "run_id",
            "storage_path",
        }:
            raise SSHTransportError(
                "remote-protocol",
                "Cluster result response schema is invalid.",
                ambiguous=True,
            )
        digest = result["bundle_digest"]
        if type(digest) is not str or re.fullmatch(r"sha256:[0-9a-f]{64}", digest) is None:
            raise SSHTransportError(
                "remote-protocol",
                "Cluster result digest is invalid.",
                ambiguous=True,
            )
        if (
            result["run_id"] != arguments.get("run_id")
            or result["storage_path"] != arguments.get("storage_path")
        ):
            raise SSHTransportError(
                "remote-protocol",
                "Cluster result response changed the run binding.",
                ambiguous=True,
            )
        expected_root = (
            transport.staging_root
            / "results"
            / "sha256"
            / digest.removeprefix("sha256:")
            / "download"
        )
        if result["remote_root"] != str(expected_root):
            raise SSHTransportError(
                "remote-protocol",
                "Cluster result response contains an unsafe object path.",
                ambiguous=True,
            )
        return result
    if operation == "commit-upload":
        if set(result) != {"bundle_digest", "object_path", "upload_id"}:
            raise SSHTransportError(
                "remote-protocol",
                "Cluster commit response schema is invalid.",
                ambiguous=True,
            )
        _canonical_uuid4(result["upload_id"], field="upload ID")
        digest = result["bundle_digest"]
        if (
            type(digest) is not str
            or re.fullmatch(r"sha256:[0-9a-f]{64}", digest) is None
        ):
            raise SSHTransportError(
                "remote-protocol",
                "Cluster commit response contains an invalid bundle digest.",
                ambiguous=True,
            )
        expected = (
            transport.staging_root
            / "objects"
            / "sha256"
            / digest.removeprefix("sha256:")
            / "submission"
        )
        if result["object_path"] != str(expected):
            raise SSHTransportError(
                "remote-protocol",
                "Cluster commit response contains an unsafe object path.",
                ambiguous=True,
            )
        return result
    if operation == "submit":
        if set(result) != {"run_id", "storage_path"}:
            raise SSHTransportError(
                "remote-protocol",
                "Cluster submit response schema is invalid.",
                ambiguous=True,
            )
        try:
            canonical = validate_run_id(result["run_id"])
        except (TypeError, ValueError) as exc:
            raise SSHTransportError(
                "remote-protocol",
                "Cluster submit response contains an invalid run ID.",
                ambiguous=True,
            ) from exc
        storage_path = result["storage_path"]
        if (
            canonical != result["run_id"]
            or type(storage_path) is not str
            or str(PurePosixPath(storage_path)) != storage_path
            or not PurePosixPath(storage_path).is_absolute()
            or storage_path.startswith("//")
        ):
            raise SSHTransportError(
                "remote-protocol",
                "Cluster submit response contains invalid identifiers.",
                ambiguous=True,
            )
        return result
    raise SSHTransportError(
        "remote-protocol",
        "Cluster response names an unsupported operation.",
        ambiguous=True,
    )


def _decode_response(
    encoded: bytes,
    request_id: str,
    operation: str,
    transport: SSHSubmissionTransport,
    arguments: Mapping[str, Any],
) -> dict[str, Any]:
    if len(encoded) > MAX_RESPONSE_BYTES:
        raise SSHTransportError(
            "remote-protocol",
            "Cluster response exceeds the protocol byte limit.",
        )
    try:
        value = json.loads(
            encoded.decode("utf-8"),
            object_pairs_hook=_reject_response_duplicates,
            parse_constant=_reject_response_nonfinite,
        )
        _check_tree(value)
    except SSHTransportError:
        raise
    except ClusterProtocolFailure as exc:
        raise SSHTransportError(
            "remote-protocol",
            "Cluster response exceeds the protocol structure limits.",
            ambiguous=True,
        ) from exc
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
        return _validate_success_result(
            operation,
            value["result"],
            transport,
            arguments,
        )
    error = value["error"]
    if (
        value["ok"] is not False
        or value["result"] is not None
        or type(error) is not dict
        or set(error) != {"code", "message", "retryable"}
        or type(error["code"]) is not str
        or re.fullmatch(r"[a-z][a-z0-9-]{0,63}", error["code"]) is None
        or type(error["message"]) is not str
        or type(error["retryable"]) is not bool
    ):
        raise SSHTransportError(
            "remote-protocol",
            "Cluster error response schema is invalid.",
            ambiguous=True,
        )
    raise SSHTransportError(
        f"remote-{error['code']}",
        str(error["message"]),
        ambiguous=error["retryable"],
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
    return _decode_response(
        completed.stdout,
        request_id,
        operation,
        transport,
        arguments,
    )


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
        relative = remote.relative_to(staging / ".partial")
        if len(relative.parts) != 1:
            raise ValueError("upload path is not one allocated directory")
        _canonical_uuid4(relative.parts[0], field="upload ID")
    except (SSHTransportError, ValueError) as exc:
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
    pre_launch: PreLaunchScript | None = None,
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
            pre_launch=pre_launch,
        ) as bundle:
            from .prepared_transport import submit_prepared_cluster_bundle

            return submit_prepared_cluster_bundle(
                bundle,
                transport=transport,
                storage_path=workflow.storage_path.as_posix(),
            )
    except SSHTransportError:
        raise
    except (OSError, TypeError, ValueError) as exc:
        raise SSHTransportError(
            "local-packaging",
            f"Local submission packaging failed: {type(exc).__name__}.",
        ) from exc
