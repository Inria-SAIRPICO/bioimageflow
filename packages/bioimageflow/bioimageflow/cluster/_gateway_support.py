"""Low-level filesystem, archive, and child-process gateway support."""

from __future__ import annotations

import hashlib
import json
import os
import re
import signal
import stat
import subprocess
import tempfile
import unicodedata
import uuid
import zipfile
from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from typing import Any

from bioimageflow.storage import canonical_json_bytes

from ._common import DIGEST_RE, canonical_digest, normalized_cluster_path, thaw_json
from ._gateway_scripts import _ATTESTATION_SCRIPT
from .protocol import GATEWAY_VERSION, MAX_REQUEST_BYTES

ROOT_NAMESPACES = (
    "gateway",
    "deployments",
    "objects",
    "operations",
    "runs",
    "transfers",
    "results",
    "temporary",
)
RECEIPT_SCHEMA = "bioimageflow.cluster.operation_receipt.v1"
_OPERATION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@-]{0,127}$")
_UPLOAD_TOKEN_RE = re.compile(r"^[0-9a-f]{32}$")
_MAX_UPLOAD_BYTES = 16 * 1024 * 1024 * 1024
_MAX_ARCHIVE_ENTRIES = 100_000
_MAX_ARCHIVE_EXPANDED_BYTES = 16 * 1024 * 1024 * 1024
_MAX_ATTESTATION_BYTES = 64 * 1024
_MAX_CHILD_RESPONSE_BYTES = MAX_REQUEST_BYTES

class GatewayOperationFailure(RuntimeError):
    """A sanitized operation failure suitable for a public response."""

    def __init__(
        self,
        category: str,
        message: str,
        *,
        phase: str = "gateway",
        allocation_state: str = "none",
        retry_safety: str = "safe",
        next_action: str = "retry-operation",
        identities: Mapping[str, str] | None = None,
    ) -> None:
        self.diagnostic = {
            "schema": "bioimageflow.cluster_diagnostic.v1",
            "phase": phase,
            "category": category,
            "message": message,
            "allocation_state": allocation_state,
            "retry_safety": retry_safety,
            "next_action": next_action,
            "identities": dict(identities or {}),
        }
        super().__init__(message)


def _failure(category: str, message: str, **kwargs: Any) -> GatewayOperationFailure:
    return GatewayOperationFailure(category, message, **kwargs)


def _stat_private_directory(path: Path) -> os.stat_result:
    try:
        metadata = path.stat(follow_symlinks=False)
    except FileNotFoundError as exc:
        raise _failure("cluster-root-unsafe", "A managed directory is missing.") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise _failure(
            "cluster-root-unsafe", "A managed path is not a real directory."
        )
    if metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) & 0o022:
        raise _failure(
            "cluster-root-unsafe", "A managed directory has unsafe ownership or mode."
        )
    return metadata


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_private_json(path: Path, value: Mapping[str, Any]) -> None:
    """Durably replace one private JSON record in its current namespace."""
    _stat_private_directory(path.parent)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.partial")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(temporary, flags, 0o600)
    try:
        encoded = canonical_json_bytes(value)
        offset = 0
        while offset < len(encoded):
            offset += os.write(descriptor, encoded[offset:])
        os.fsync(descriptor)
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or metadata.st_nlink != 1
        ):
            raise _failure(
                "operation-record-tampered", "A receipt candidate is unsafe."
            )
    finally:
        os.close(descriptor)
    try:
        os.replace(temporary, path)
        os.chmod(path, 0o600, follow_symlinks=False)
        _fsync_directory(path.parent)
    except BaseException:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise


def _atomic_private_bytes(path: Path, content: bytes, mode: int) -> None:
    _stat_private_directory(path.parent)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.partial")
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        mode,
    )
    try:
        offset = 0
        while offset < len(content):
            offset += os.write(descriptor, content[offset:])
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    try:
        os.replace(temporary, path)
        os.chmod(path, mode, follow_symlinks=False)
        _fsync_directory(path.parent)
    except BaseException:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise


def _exact_arguments(value: Mapping[str, Any], fields: set[str]) -> dict[str, Any]:
    if type(value) is not dict and not isinstance(value, Mapping):
        raise _failure("protocol-incompatible", "Operation arguments must be an object.")
    result = thaw_json(value)
    if type(result) is not dict:
        raise _failure("protocol-incompatible", "Operation arguments must be an object.")
    if set(result) != fields:
        raise _failure(
            "protocol-incompatible", "Operation arguments have missing or unknown fields."
        )
    return result


def _file_digest(descriptor: int) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    os.lseek(descriptor, 0, os.SEEK_SET)
    while chunk := os.read(descriptor, 1024 * 1024):
        size += len(chunk)
        digest.update(chunk)
    return size, f"sha256:{digest.hexdigest()}"


def _validate_published_file(path: Path, expected_digest: str) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) & 0o077
            or metadata.st_nlink != 1
        ):
            raise _failure("deployment-tampered", "A published artifact is unsafe.")
        _, digest = _file_digest(descriptor)
    finally:
        os.close(descriptor)
    if digest != expected_digest:
        raise _failure("deployment-tampered", "A published artifact digest changed.")


def _validate_deployment_archive(
    path: Path, deployment_id: str, manifest_digest: str
) -> dict[str, Any]:
    """Validate a deployment ZIP without extracting or trusting archive metadata."""
    try:
        archive = zipfile.ZipFile(path)
    except (OSError, zipfile.BadZipFile) as exc:
        raise _failure("deployment-tampered", "The deployment archive is invalid.") from exc
    with archive:
        members = archive.infolist()
        if len(members) > _MAX_ARCHIVE_ENTRIES:
            raise _failure(
                "resource-limit-exceeded", "The deployment archive has too many entries."
            )
        names: set[str] = set()
        total = 0
        manifest_member: zipfile.ZipInfo | None = None
        for member in members:
            name = member.filename
            normalized = unicodedata.normalize("NFC", name)
            parts = PurePosixPath(name).parts
            unix_type = (member.external_attr >> 16) & 0o170000
            if (
                not name
                or name != normalized
                or name.startswith("/")
                or "\\" in name
                or any(part in {"", ".", ".."} for part in parts)
                or normalized in names
                or member.flag_bits & 0x1
                or unix_type not in {0, stat.S_IFREG, stat.S_IFDIR}
            ):
                raise _failure(
                    "deployment-tampered", "The deployment archive has an unsafe member."
                )
            names.add(normalized)
            total += member.file_size
            if total > _MAX_ARCHIVE_EXPANDED_BYTES:
                raise _failure(
                    "resource-limit-exceeded", "The deployment archive expands too large."
                )
            if member.compress_size == 0 and member.file_size:
                raise _failure(
                    "resource-limit-exceeded", "The deployment archive ratio is unsafe."
                )
            if member.compress_size and member.file_size / member.compress_size > 1000:
                raise _failure(
                    "resource-limit-exceeded", "The deployment archive ratio is unsafe."
                )
            if name == "deployment-manifest.json":
                manifest_member = member
        if manifest_member is None or manifest_member.file_size > 4 * 1024 * 1024:
            raise _failure(
                "deployment-tampered", "The deployment archive has no bounded manifest."
            )
        try:
            manifest = json.loads(archive.read(manifest_member))
        except (UnicodeDecodeError, json.JSONDecodeError, RuntimeError) as exc:
            raise _failure(
                "deployment-tampered", "The deployment manifest is malformed."
            ) from exc
        if type(manifest) is not dict:
            raise _failure(
                "deployment-tampered", "The deployment manifest identity does not match."
            )
        identity = {
            key: item
            for key, item in manifest.items()
            if key not in {"deployment_id", "manifest_digest"}
        }
        computed = canonical_digest(identity)
        if (
            manifest.get("deployment_id") != computed
            or manifest.get("manifest_digest") != computed
            or deployment_id != computed
            or manifest_digest != computed
        ):
            raise _failure(
                "deployment-tampered", "The deployment manifest identity does not match."
            )
        return manifest


def _entry_digest(manifest: Mapping[str, Any], logical_path: str) -> str:
    entries = manifest.get("content_entries")
    if type(entries) is not list:
        raise _failure("deployment-tampered", "The deployment file manifest is invalid.")
    matches = [
        item
        for item in entries
        if type(item) is dict
        and item.get("path") == logical_path
        and item.get("kind") == "file"
    ]
    if (
        len(matches) != 1
        or type(matches[0].get("digest")) is not str
        or DIGEST_RE.fullmatch(matches[0]["digest"]) is None
    ):
        raise _failure(
            "deployment-tampered", "A required deployment file is not manifested."
        )
    return matches[0]["digest"]


def _extract_deployment_archive(path: Path, destination: Path) -> None:
    """Extract an already validated deployment ZIP into one new private directory."""
    destination.mkdir(mode=0o700)
    with zipfile.ZipFile(path) as archive:
        for member in archive.infolist():
            relative = PurePosixPath(member.filename)
            target = destination.joinpath(*relative.parts)
            if member.is_dir():
                target.mkdir(mode=0o700, parents=True, exist_ok=True)
                continue
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            descriptor = os.open(
                target,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            try:
                with archive.open(member) as source:
                    while chunk := source.read(1024 * 1024):
                        offset = 0
                        while offset < len(chunk):
                            offset += os.write(descriptor, chunk[offset:])
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    _fsync_directory(destination)


def _verify_extracted_deployment(
    destination: Path, manifest: Mapping[str, Any]
) -> None:
    entries = manifest.get("content_entries")
    if type(entries) is not list:
        raise _failure("deployment-tampered", "The deployment file manifest is invalid.")
    expected: set[str] = set()
    for entry in entries:
        if (
            type(entry) is not dict
            or set(entry) != {"digest", "kind", "path", "size"}
            or type(entry["path"]) is not str
            or entry["kind"] not in {"file", "directory"}
            or type(entry["size"]) is not int
            or type(entry["digest"]) is not str
            or DIGEST_RE.fullmatch(entry["digest"]) is None
        ):
            raise _failure(
                "deployment-tampered", "The deployment file manifest is invalid."
            )
        relative = PurePosixPath(entry["path"])
        if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
            raise _failure(
                "deployment-tampered", "The deployment file manifest path is unsafe."
            )
        target = destination.joinpath(*relative.parts)
        metadata = target.stat(follow_symlinks=False)
        if entry["kind"] == "directory":
            if not stat.S_ISDIR(metadata.st_mode):
                raise _failure(
                    "deployment-tampered", "A deployment directory changed kind."
                )
        else:
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_nlink != 1
                or metadata.st_size != entry["size"]
            ):
                raise _failure(
                    "deployment-tampered", "A deployment file changed identity."
                )
            descriptor = os.open(target, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            try:
                _, digest = _file_digest(descriptor)
            finally:
                os.close(descriptor)
            if digest != entry["digest"]:
                raise _failure(
                    "deployment-tampered", "A deployment file digest does not match."
                )
        expected.add(entry["path"])
    actual = {
        item.relative_to(destination).as_posix()
        for item in destination.rglob("*")
        if item.relative_to(destination).as_posix() != "deployment-manifest.json"
    }
    if actual != expected:
        raise _failure(
            "deployment-tampered", "The extracted deployment inventory does not match."
        )


def _child_environment(extra: Mapping[str, str] | None = None) -> dict[str, str]:
    names = (
        "PATH",
        "LANG",
        "LC_ALL",
        "LD_LIBRARY_PATH",
        "DYLD_LIBRARY_PATH",
        "LIBRARY_PATH",
        "PYTHONHOME",
    )
    environment = {name: os.environ[name] for name in names if name in os.environ}
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment.update(extra or {})
    return environment


def _run_json_child(
    argv: list[str],
    *,
    environment: Mapping[str, str],
    timeout: float,
    input_bytes: bytes = b"",
    failure_category: str = "deployment-install-failed",
    output_limit: int = _MAX_ATTESTATION_BYTES,
) -> dict[str, Any]:
    if type(output_limit) is not int or not 1 <= output_limit <= _MAX_CHILD_RESPONSE_BYTES:
        raise ValueError("output_limit is invalid")
    stdout_file = tempfile.TemporaryFile()
    stderr_file = tempfile.TemporaryFile()
    try:
        process = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=stdout_file,
            stderr=stderr_file,
            shell=False,
            env=dict(environment),
            start_new_session=True,
        )
        try:
            process.communicate(input=input_bytes, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
            raise _failure(
                failure_category,
                "The external Python verification process timed out.",
            ) from exc
        stdout_file.seek(0)
        encoded = stdout_file.read(output_limit + 1)
        stderr_file.seek(0)
        # Read and discard only a bounded diagnostic prefix. Child stderr is
        # trusted-code output and is never returned or persisted by the gateway.
        stderr_file.read(_MAX_ATTESTATION_BYTES + 1)
    except OSError as exc:
        raise _failure(
            failure_category,
            "The external Python verification process could not complete.",
        ) from exc
    finally:
        stdout_file.close()
        stderr_file.close()
    if process.returncode != 0 or len(encoded) > output_limit:
        raise _failure(
            failure_category,
            "The external Python verification process failed.",
        )
    try:
        value = json.loads(encoded)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _failure(
            failure_category,
            "The external Python verification response is malformed.",
        ) from exc
    if type(value) is not dict:
        raise _failure(
            failure_category,
            "The external Python verification response is malformed.",
        )
    return value


def _attest_existing_python(
    manifest: Mapping[str, Any],
    *,
    timeout: float = 30.0,
    failure_category: str = "deployment-install-failed",
) -> tuple[dict[str, Any], str]:
    environment = manifest.get("environment")
    scheduler = manifest.get("scheduler")
    if (
        type(environment) is not dict
        or environment.get("kind") != "existing_python"
        or type(environment.get("source")) is not str
        or scheduler not in {"slurm", "pbs", "lsf"}
    ):
        raise _failure(
            failure_category,
            "Only an existing-Python deployment can use external attestation.",
        )
    try:
        executable = Path(
            str(
                normalized_cluster_path(
                    environment["source"], field="external_python"
                )
            )
        )
    except (TypeError, ValueError) as exc:
        raise _failure(
            failure_category,
            "The configured external Python path is not normalized.",
        ) from exc
    if (
        not executable.is_absolute()
        or not executable.is_file()
        or not os.access(executable, os.X_OK)
    ):
        raise _failure(
            failure_category,
            "The configured external Python is not an executable regular file.",
        )
    requested = str(executable)
    resolved = str(executable.resolve())
    before = executable.stat()
    path_before = executable.lstat()
    attestation = _run_json_child(
        [requested, "-I", "-c", _ATTESTATION_SCRIPT],
        environment=_child_environment({"BIOIMAGEFLOW_REQUESTED_PYTHON": requested}),
        timeout=timeout,
    )
    after = executable.stat()
    path_after = executable.lstat()
    if (
        before.st_dev,
        before.st_ino,
        before.st_mode,
        before.st_size,
        before.st_mtime_ns,
    ) != (
        after.st_dev,
        after.st_ino,
        after.st_mode,
        after.st_size,
        after.st_mtime_ns,
    ):
        raise _failure(
            "external-environment-changed",
            "The external Python changed during attestation.",
        )
    if (
        path_before.st_dev,
        path_before.st_ino,
        path_before.st_mode,
        path_before.st_size,
        path_before.st_mtime_ns,
    ) != (
        path_after.st_dev,
        path_after.st_ino,
        path_after.st_mode,
        path_after.st_size,
        path_after.st_mtime_ns,
    ):
        raise _failure(
            "external-environment-changed",
            "The external Python path changed during attestation.",
        )
    attestation["executable_stat"] = {
        "device": before.st_dev,
        "inode": before.st_ino,
        "mode": before.st_mode,
        "uid": before.st_uid,
        "links": before.st_nlink,
        "size": before.st_size,
        "mtime_ns": before.st_mtime_ns,
    }
    attestation["requested_path_stat"] = {
        "device": path_before.st_dev,
        "inode": path_before.st_ino,
        "mode": path_before.st_mode,
        "uid": path_before.st_uid,
        "links": path_before.st_nlink,
        "size": path_before.st_size,
        "mtime_ns": path_before.st_mtime_ns,
    }
    fields = {
        "schema",
        "requested_executable",
        "resolved_executable",
        "implementation",
        "cache_tag",
        "version",
        "version_info",
        "platform_system",
        "platform_machine",
        "packages",
        "distribution_metadata",
        "missing_packages",
        "psij_executor_names",
        "psij_distributions",
        "executable_stat",
        "requested_path_stat",
    }
    if (
        set(attestation) != fields
        or attestation.get("schema")
        != "bioimageflow.cluster.existing_python_attestation.v1"
        or attestation.get("requested_executable") != requested
        or attestation.get("resolved_executable") != resolved
        or type(attestation.get("packages")) is not dict
        or type(attestation.get("distribution_metadata")) is not dict
        or type(attestation.get("missing_packages")) is not list
        or type(attestation.get("psij_executor_names")) is not list
        or type(attestation.get("psij_distributions")) is not dict
        or attestation["missing_packages"]
        or scheduler not in attestation["psij_executor_names"]
        or attestation["packages"].get("bioimageflow")
        != manifest.get("bioimageflow_version")
    ):
        raise _failure(
            failure_category,
            "The external Python lacks the exact required runtime or scheduler plugin.",
        )
    return attestation, canonical_digest(attestation)


_RUN_RECORD_SCHEMA = "bioimageflow.cluster.managed_run_record.v1"
_RUN_PHASES = frozenset(
    {
        "allocated",
        "uploading",
        "ready",
        "scheduler-intent",
        "submitted",
        "rejected",
        "cancelled",
        "uncertain",
    }
)


def _current_gateway_binding() -> tuple[str, str]:
    publication_id = os.environ.get(
        "BIOIMAGEFLOW_GATEWAY_PUBLICATION_ID", "in-process-gateway"
    )
    artifact_digest = os.environ.get("BIOIMAGEFLOW_GATEWAY_ARTIFACT_DIGEST")
    if artifact_digest is None:
        artifact_digest = canonical_digest(
            {
                "schema": "bioimageflow.cluster.in_process_gateway.v1",
                "gateway_version": GATEWAY_VERSION,
            }
        )
    if (
        not publication_id
        or DIGEST_RE.fullmatch(artifact_digest) is None
    ):
        raise _failure("gateway-untrusted", "The active gateway binding is invalid.")
    return publication_id, artifact_digest
