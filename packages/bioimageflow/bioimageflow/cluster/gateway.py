"""Installed one-shot managed-cluster gateway and durable receipt store."""

from __future__ import annotations

import json
import hashlib
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import unicodedata
import uuid
import zipfile
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from bioimageflow.storage import canonical_json_bytes

from ._common import DIGEST_RE, canonical_digest, normalized_cluster_path
from .protocol import (
    GATEWAY_VERSION,
    MAX_REQUEST_BYTES,
    PROTOCOL_VERSION,
    GatewayProtocolError,
    GatewayRequest,
    GatewayResponse,
)


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

_ATTESTATION_SCRIPT = r'''from __future__ import annotations
import importlib.metadata as metadata
import hashlib
import json
import os
import platform
import sys

required = ("bioimageflow", "bioimageflow-core", "parsl", "psij-python")
packages = {}
distribution_metadata = {}
missing = []
for name in required:
    try:
        distribution = metadata.distribution(name)
        packages[name] = distribution.version
        hashes = {}
        for item in distribution.files or ():
            if item.name not in {"METADATA", "RECORD"}:
                continue
            digest = hashlib.sha256()
            size = 0
            with distribution.locate_file(item).open("rb") as stream:
                while chunk := stream.read(1024 * 1024):
                    size += len(chunk)
                    if size > 64 * 1024 * 1024:
                        raise RuntimeError("distribution metadata exceeds attestation limit")
                    digest.update(chunk)
            key = item.name.lower()
            hashes[key + "_sha256"] = "sha256:" + digest.hexdigest()
            hashes[key + "_size"] = size
        distribution_metadata[name] = hashes
    except metadata.PackageNotFoundError:
        missing.append(name)
try:
    import psij
    executor_names = sorted(psij.JobExecutor.get_executor_names())
except Exception:
    executor_names = []
psij_distributions = {}
for distribution in metadata.distributions():
    name = distribution.metadata.get("Name") or ""
    if "psij" in name.lower():
        psij_distributions[name] = distribution.version
value = {
    "schema": "bioimageflow.cluster.existing_python_attestation.v1",
    "requested_executable": os.environ["BIOIMAGEFLOW_REQUESTED_PYTHON"],
    "resolved_executable": os.path.realpath(sys.executable),
    "implementation": sys.implementation.name,
    "cache_tag": sys.implementation.cache_tag,
    "version": platform.python_version(),
    "version_info": list(sys.version_info[:3]),
    "platform_system": platform.system(),
    "platform_machine": platform.machine(),
    "packages": packages,
    "distribution_metadata": distribution_metadata,
    "missing_packages": sorted(missing),
    "psij_executor_names": executor_names,
    "psij_distributions": dict(sorted(psij_distributions.items())),
}
print(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False))
'''

_FACTORY_VALIDATOR_SCRIPT = r'''from __future__ import annotations
import json
import os
import sys
from pathlib import Path, PurePosixPath


def main():
    request_path = Path(sys.argv[1])
    request = json.loads(request_path.read_bytes())
    encoded_secrets = sys.stdin.buffer.read(256 * 1024 + 1)
    if len(encoded_secrets) > 256 * 1024:
        raise RuntimeError("secret handoff exceeds validation limit")
    secret_values = json.loads(encoded_secrets or b"{}")
    if not isinstance(secret_values, dict):
        raise RuntimeError("secret handoff must be an object")
    content = Path(request["content_root"])
    parsl_root = content / "parsl"
    sys.path.insert(0, str(parsl_root))
    include = parsl_root / "include"
    if include.is_dir():
        for child in sorted(include.iterdir()):
            if child.is_dir():
                sys.path.insert(0, str(child))
    from bioimageflow.parsl.factory import ParslFactoryRuntime, managed_worker_init
    from bioimageflow.parsl.managed_validation import validate_managed_factory
    from bioimageflow.parsl.startup import CORE_REQUIREMENT

    factory = request["parsl"]
    reference = (
        "factory:" + factory["factory"]
        if factory["source_kind"] == "file"
        else factory["source"]
    )
    deployment_root = PurePosixPath(request["deployment_root"])
    setup = content / "setup" / "setup.sh"
    runtime = ParslFactoryRuntime(
        deployment_root=deployment_root,
        deployment_id=request["deployment_id"],
        worker_init=managed_worker_init(
            setup_path=PurePosixPath(str(setup)) if setup.is_file() else None,
            activation_path=deployment_root / "activation.sh",
            deployment_id=request["deployment_id"],
        ),
        environment_name="existing-python",
        environment_identity=request["attestation_digest"],
        core_requirement=CORE_REQUIREMENT,
    )
    report = validate_managed_factory(
        reference,
        runtime=runtime,
        orchestrator_scheduler=request["scheduler"],
        kwargs=factory["kwargs"],
        secret_refs=factory["secret_refs"],
        secret_values=secret_values,
        timeout=request["timeout"],
    )
    secret_values.clear()
    print(json.dumps(report.to_dict(), sort_keys=True, separators=(",", ":")))


if __name__ == "__main__":
    main()
'''


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
    result = dict(value)
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
        if (
            type(manifest) is not dict
            or manifest.get("deployment_id") != deployment_id
            or manifest.get("manifest_digest") != manifest_digest
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
) -> dict[str, Any]:
    try:
        completed = subprocess.run(
            argv,
            input=input_bytes,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            check=False,
            timeout=timeout,
            env=dict(environment),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise _failure(
            failure_category,
            "The external Python verification process could not complete.",
        ) from exc
    if completed.returncode != 0 or len(completed.stdout) > _MAX_ATTESTATION_BYTES:
        raise _failure(
            failure_category,
            "The external Python verification process failed.",
        )
    try:
        value = json.loads(completed.stdout)
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


class GatewayState:
    """Validated cluster root and idempotent gateway operation journal."""

    def __init__(self, root: str | PurePosixPath | Path) -> None:
        normalized = normalized_cluster_path(str(root), field="root")
        self.root = Path(str(normalized))
        self.validate_layout()

    @classmethod
    def initialize(cls, root: str | PurePosixPath | Path) -> "GatewayState":
        """Create only a missing final root component and its private layout."""
        normalized = normalized_cluster_path(str(root), field="root")
        path = Path(str(normalized))
        if not path.exists() and not path.is_symlink():
            parent = path.parent
            parent_metadata = parent.stat(follow_symlinks=False)
            if stat.S_ISLNK(parent_metadata.st_mode) or not stat.S_ISDIR(
                parent_metadata.st_mode
            ):
                raise _failure(
                    "cluster-root-unsafe", "The cluster root parent is unsafe."
                )
            parent_fd = os.open(
                parent,
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0),
            )
            try:
                os.mkdir(path.name, 0o700, dir_fd=parent_fd)
                os.fsync(parent_fd)
            finally:
                os.close(parent_fd)
        _stat_private_directory(path)
        root_fd = os.open(
            path,
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            for name in ROOT_NAMESPACES:
                try:
                    os.mkdir(name, 0o700, dir_fd=root_fd)
                except FileExistsError:
                    pass
            os.fsync(root_fd)
        finally:
            os.close(root_fd)
        return cls(path)

    def validate_layout(self) -> None:
        _stat_private_directory(self.root)
        for name in ROOT_NAMESPACES:
            _stat_private_directory(self.root / name)

    def _private_subdirectory(self, parent: Path, name: str) -> Path:
        _stat_private_directory(parent)
        descriptor = os.open(
            parent,
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            try:
                os.mkdir(name, 0o700, dir_fd=descriptor)
                os.fsync(descriptor)
            except FileExistsError:
                pass
        finally:
            os.close(descriptor)
        path = parent / name
        _stat_private_directory(path)
        return path

    def allocate_upload(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        value = _exact_arguments(arguments, {"size", "digest", "kind"})
        if (
            type(value["size"]) is not int
            or not 0 <= value["size"] <= _MAX_UPLOAD_BYTES
            or type(value["digest"]) is not str
            or DIGEST_RE.fullmatch(value["digest"]) is None
            or value["kind"] not in {"deployment", "invocation", "result"}
        ):
            raise _failure(
                "resource-limit-exceeded", "The requested upload manifest is invalid."
            )
        uploads = self._private_subdirectory(self.root / "temporary", "uploads")
        token = uuid.uuid4().hex
        path = uploads / f"{token}.partial"
        descriptor = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        _fsync_directory(uploads)
        return {"upload_token": token, "upload_path": str(path)}

    def commit_upload(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        value = _exact_arguments(arguments, {"upload_token", "size", "digest"})
        token = value["upload_token"]
        if (
            type(token) is not str
            or _UPLOAD_TOKEN_RE.fullmatch(token) is None
            or type(value["size"]) is not int
            or not 0 <= value["size"] <= _MAX_UPLOAD_BYTES
            or type(value["digest"]) is not str
            or DIGEST_RE.fullmatch(value["digest"]) is None
        ):
            raise _failure("protocol-incompatible", "The upload commit is invalid.")
        uploads = self._private_subdirectory(self.root / "temporary", "uploads")
        partial = uploads / f"{token}.partial"
        try:
            descriptor = os.open(
                partial, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            )
        except FileNotFoundError as exc:
            raise _failure(
                "environment-artifact-missing", "The allocated upload is missing."
            ) from exc
        try:
            before = os.fstat(descriptor)
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_uid != os.geteuid()
                or stat.S_IMODE(before.st_mode) & 0o077
                or before.st_nlink != 1
            ):
                raise _failure("deployment-tampered", "The upload candidate is unsafe.")
            size, digest = _file_digest(descriptor)
            after = os.fstat(descriptor)
        finally:
            os.close(descriptor)
        if (
            (before.st_dev, before.st_ino, before.st_size)
            != (after.st_dev, after.st_ino, after.st_size)
            or size != value["size"]
            or digest != value["digest"]
        ):
            raise _failure(
                "deployment-tampered", "The upload size or digest does not match."
            )
        object_path = self.root / "objects" / f"{digest[7:]}.object"
        try:
            os.link(partial, object_path, follow_symlinks=False)
        except FileExistsError:
            _validate_published_file(object_path, digest)
        else:
            _fsync_directory(object_path.parent)
        partial.unlink()
        _fsync_directory(uploads)
        _validate_published_file(object_path, digest)
        return {"object_id": digest, "size": size}

    def publish_deployment(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        value = _exact_arguments(
            arguments, {"object_id", "deployment_id", "manifest_digest"}
        )
        if any(
            type(value[name]) is not str or DIGEST_RE.fullmatch(value[name]) is None
            for name in value
        ):
            raise _failure(
                "protocol-incompatible", "Deployment publication identities are invalid."
            )
        object_path = self.root / "objects" / f"{value['object_id'][7:]}.object"
        _validate_published_file(object_path, value["object_id"])
        prepared_deployment_id = value["deployment_id"]
        manifest = _validate_deployment_archive(
            object_path, prepared_deployment_id, value["manifest_digest"]
        )
        environment = manifest.get("environment")
        external = type(environment) is dict and environment.get("kind") == "existing_python"
        attestation: dict[str, Any] | None = None
        attestation_digest: str | None = None
        activation: bytes | None = None
        if external:
            attestation, attestation_digest = _attest_existing_python(manifest)
            executable = attestation["requested_executable"]
            activation = (
                "set -eu\n"
                f"export PATH={shlex.quote(str(Path(executable).parent))}:\"$PATH\"\n"
                f"export BIOIMAGEFLOW_EXTERNAL_PYTHON={shlex.quote(executable)}\n"
            ).encode()
        deployment_id = (
            canonical_digest(
                {
                    "schema": "bioimageflow.cluster.external_deployment_identity.v1",
                    "prepared_deployment_id": prepared_deployment_id,
                    "external_attestation_digest": attestation_digest,
                }
            )
            if external
            else prepared_deployment_id
        )
        destination = self.root / "deployments" / deployment_id[7:]
        publication = {
            "schema": "bioimageflow.cluster.deployment_publication.v1",
            "deployment_id": deployment_id,
            "prepared_deployment_id": prepared_deployment_id,
            "manifest_digest": value["manifest_digest"],
            "object_id": value["object_id"],
            "state": "published",
            "environment_installed": external,
            "external_attestation_digest": attestation_digest,
            "external_attestation": attestation,
            "activation_digest": (
                None
                if activation is None
                else f"sha256:{hashlib.sha256(activation).hexdigest()}"
            ),
            "factory_validator_digest": (
                f"sha256:{hashlib.sha256(_FACTORY_VALIDATOR_SCRIPT.encode()).hexdigest()}"
                if external
                else None
            ),
        }
        if destination.exists() or destination.is_symlink():
            _stat_private_directory(destination)
            record = destination / "publication.json"
            try:
                observed = json.loads(record.read_bytes())
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise _failure(
                    "deployment-tampered", "The deployment publication is malformed."
                ) from exc
            _validate_published_file(destination / "artifact.zip", value["object_id"])
            if observed != publication:
                raise _failure(
                    "deployment-tampered", "A different deployment is already published."
                )
            return {**publication, "reused": True, "gateway_publication_id": "gateway-v1"}
        candidates = self._private_subdirectory(self.root / "temporary", "deployments")
        candidate = candidates / f"{deployment_id[7:]}.{uuid.uuid4().hex}"
        candidate.mkdir(mode=0o700)
        try:
            artifact = candidate / "artifact.zip"
            source_descriptor = os.open(
                object_path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            )
            destination_descriptor = os.open(
                artifact,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            try:
                while chunk := os.read(source_descriptor, 1024 * 1024):
                    offset = 0
                    while offset < len(chunk):
                        offset += os.write(destination_descriptor, chunk[offset:])
                os.fsync(destination_descriptor)
            finally:
                os.close(source_descriptor)
                os.close(destination_descriptor)
            _validate_published_file(artifact, value["object_id"])
            if external:
                content = candidate / "content"
                _extract_deployment_archive(artifact, content)
                _verify_extracted_deployment(content, manifest)
                assert attestation is not None
                assert activation is not None
                _atomic_private_bytes(candidate / "activation.sh", activation, 0o600)
                _atomic_private_bytes(
                    candidate / "validate_factory.py",
                    _FACTORY_VALIDATOR_SCRIPT.encode(),
                    0o600,
                )
            _atomic_private_json(candidate / "publication.json", publication)
            _fsync_directory(candidate)
            try:
                os.rename(candidate, destination)
            except FileExistsError as exc:
                raise _failure(
                    "deployment-tampered", "Deployment publication raced another writer."
                ) from exc
        except BaseException:
            shutil.rmtree(candidate, ignore_errors=True)
            raise
        _fsync_directory(destination.parent)
        return {**publication, "reused": False, "gateway_publication_id": "gateway-v1"}

    def validate_deployment(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        value = _exact_arguments(
            arguments, {"deployment_id", "scheduler_job", "timeout"}
        )
        deployment_id = value["deployment_id"]
        if type(deployment_id) is not str or DIGEST_RE.fullmatch(deployment_id) is None:
            raise _failure("protocol-incompatible", "deployment_id is invalid.")
        destination = self.root / "deployments" / deployment_id[7:]
        _stat_private_directory(destination)
        try:
            publication = json.loads((destination / "publication.json").read_bytes())
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise _failure(
                "deployment-tampered", "The deployment publication is malformed."
            ) from exc
        if publication.get("deployment_id") != deployment_id:
            raise _failure("deployment-tampered", "The deployment identity changed.")
        _validate_published_file(destination / "artifact.zip", publication["object_id"])
        if publication.get("environment_installed") is not True:
            raise _failure(
                "deployment-install-failed",
                "Deployment bytes are published, but its environment is not installed.",
                phase="validation",
                retry_safety="safe",
                next_action="install-deployment-environment",
                identities={"deployment_id": deployment_id},
            )
        artifact = destination / "artifact.zip"
        manifest = _validate_deployment_archive(
            artifact,
            publication["prepared_deployment_id"],
            publication["manifest_digest"],
        )
        _verify_extracted_deployment(destination / "content", manifest)
        _validate_published_file(
            destination / "activation.sh", publication["activation_digest"]
        )
        _validate_published_file(
            destination / "validate_factory.py",
            publication["factory_validator_digest"],
        )
        fresh_attestation, fresh_digest = _attest_existing_python(
            manifest, failure_category="external-environment-changed"
        )
        if fresh_digest != publication.get("external_attestation_digest"):
            raise _failure(
                "external-environment-changed",
                "The external Python attestation changed after deployment confirmation.",
                phase="validation",
                retry_safety="safe",
                next_action="deploy-and-confirm-again",
                identities={"deployment_id": deployment_id},
            )
        scheduler_job = value["scheduler_job"]
        if (
            not isinstance(scheduler_job, Mapping)
            or scheduler_job.get("schema") != "bioimageflow.scheduler_job.v1"
            or scheduler_job.get("scheduler") != manifest.get("scheduler")
        ):
            raise _failure(
                "unsupported-scheduler-adapter",
                "The scheduler request does not match the deployed adapter.",
                phase="validation",
                retry_safety="safe",
                next_action="deploy-matching-scheduler",
                identities={"deployment_id": deployment_id},
            )
        timeout = 30.0 if value["timeout"] is None else value["timeout"]
        if (
            type(timeout) not in {int, float}
            or not 0 < float(timeout) <= 300
        ):
            raise _failure(
                "protocol-incompatible", "Validation timeout must be in (0, 300]."
            )
        parsl = manifest.get("parsl")
        if (
            type(parsl) is not dict
            or parsl.get("source_kind") not in {"file", "module"}
            or type(parsl.get("factory")) is not str
            or type(parsl.get("kwargs")) is not dict
            or type(parsl.get("secret_refs")) is not dict
        ):
            raise _failure(
                "deployment-tampered", "The retained Parsl configuration is invalid."
            )
        if parsl["source_kind"] == "file":
            _validate_published_file(
                destination / "content" / "parsl" / "factory.py",
                _entry_digest(manifest, "parsl/factory.py"),
            )
        secrets: dict[str, str] = {}
        secret_bytes = 0
        for reference in parsl["secret_refs"].values():
            if type(reference) is not str or re.fullmatch(
                r"[A-Za-z_][A-Za-z0-9_]*", reference
            ) is None:
                raise _failure(
                    "deployment-tampered", "A retained secret reference is invalid."
                )
            if reference in os.environ:
                secret = os.environ[reference]
                size = len(secret.encode("utf-8")) if type(secret) is str else 0
                secret_bytes += size
                if (
                    type(secret) is not str
                    or "\0" in secret
                    or size > 64 * 1024
                    or secret_bytes > 256 * 1024
                ):
                    raise _failure(
                        "resource-limit-exceeded",
                        "Resolved factory secrets exceed the validation handoff limit.",
                    )
                secrets[reference] = secret
        validations = self._private_subdirectory(self.root / "temporary", "validations")
        private = validations / uuid.uuid4().hex
        private.mkdir(mode=0o700)
        try:
            request = {
                "content_root": str(destination / "content"),
                "deployment_root": str(destination),
                "deployment_id": deployment_id,
                "attestation_digest": fresh_digest,
                "scheduler": manifest["scheduler"],
                "parsl": parsl,
                "timeout": float(timeout),
            }
            request_path = private / "request.json"
            _atomic_private_json(request_path, request)
            encoded_secrets = canonical_json_bytes(secrets)
            report = _run_json_child(
                [
                    fresh_attestation["requested_executable"],
                    "-I",
                    "-B",
                    str(destination / "validate_factory.py"),
                    str(request_path),
                ],
                environment=_child_environment(),
                timeout=float(timeout) + 10,
                input_bytes=encoded_secrets,
                failure_category="parsl-factory-failed",
            )
        finally:
            for reference in secrets:
                secrets[reference] = ""
            shutil.rmtree(private, ignore_errors=True)
        expected_report_fields = {
            "schema",
            "valid",
            "executor_labels",
            "retries",
            "executor_bindings",
            "provider_evidence",
            "diagnostics",
        }
        if (
            set(report) != expected_report_fields
            or report.get("schema") != "bioimageflow.managed_factory_validation.v1"
            or type(report.get("valid")) is not bool
            or type(report.get("executor_bindings")) is not dict
            or type(report.get("provider_evidence")) is not list
            or type(report.get("diagnostics")) is not list
        ):
            raise _failure(
                "parsl-factory-failed", "The Parsl factory validation report is invalid."
            )
        diagnostics = []
        for diagnostic in report["diagnostics"]:
            if (
                type(diagnostic) is not dict
                or set(diagnostic) != {"category", "message", "field"}
                or type(diagnostic["category"]) is not str
                or type(diagnostic["message"]) is not str
            ):
                raise _failure(
                    "parsl-factory-failed",
                    "The Parsl factory diagnostic is invalid.",
                )
            diagnostics.append(
                {
                    "schema": "bioimageflow.cluster_diagnostic.v1",
                    "phase": "validation",
                    "category": diagnostic["category"],
                    "message": diagnostic["message"],
                    "allocation_state": "none",
                    "retry_safety": "safe",
                    "next_action": "fix-parsl-factory",
                    "identities": {"deployment_id": deployment_id},
                }
            )
        payload: dict[str, Any] = {
            "schema": "bioimageflow.cluster_validation_report.v1",
            "deployment_id": deployment_id,
            "valid": report["valid"],
            "validation_digest": None,
            "expires_at": (
                datetime.now(timezone.utc) + timedelta(minutes=30)
            ).isoformat(timespec="seconds").replace("+00:00", "Z"),
            "executor_bindings": report["executor_bindings"],
            "verified_facts": [
                "login-node external Python identity",
                "required runtime distributions",
                "PSI/J scheduler descriptor",
                "non-allocating Parsl factory contract",
            ],
            "declared_facts": ["shared filesystem topology"],
            "unverified_facts": [
                "compute-node shared-root visibility",
                "worker-to-orchestrator networking",
                "nested scheduler submission policy",
                "queue availability at submission time",
                "future quota availability",
                "worker hardware availability",
            ],
            "diagnostics": diagnostics,
            "evidence": {
                "external_attestation": fresh_attestation,
                "external_attestation_digest": fresh_digest,
                "provider_evidence": report["provider_evidence"],
                "parsl_retries": report["retries"],
                "psij_executor": manifest["scheduler"],
                "gateway_version": GATEWAY_VERSION,
                "protocol_version": PROTOCOL_VERSION,
            },
        }
        payload["validation_digest"] = canonical_digest(
            {key: item for key, item in payload.items() if key != "validation_digest"}
        )
        return payload

    def _receipt_path(self, operation_id: str) -> Path:
        if (
            type(operation_id) is not str
            or _OPERATION_ID_RE.fullmatch(operation_id) is None
        ):
            raise _failure(
                "protocol-incompatible", "operation_id is not a safe stable identifier."
            )
        return self.root / "operations" / f"{operation_id}.json"

    def _read_receipt(self, operation_id: str) -> dict[str, Any] | None:
        path = self._receipt_path(operation_id)
        try:
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        except FileNotFoundError:
            return None
        try:
            before = os.fstat(descriptor)
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_uid != os.geteuid()
                or stat.S_IMODE(before.st_mode) & 0o077
                or before.st_nlink != 1
            ):
                raise _failure(
                    "operation-record-tampered", "The operation receipt is unsafe."
                )
            chunks: list[bytes] = []
            size = 0
            while chunk := os.read(descriptor, 64 * 1024):
                size += len(chunk)
                if size > 1024 * 1024:
                    raise _failure(
                        "operation-record-tampered", "The operation receipt is oversized."
                    )
                chunks.append(chunk)
            after = os.fstat(descriptor)
        finally:
            os.close(descriptor)
        if (before.st_dev, before.st_ino, before.st_size) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
        ):
            raise _failure(
                "operation-record-tampered", "The operation receipt changed while read."
            )
        try:
            value = json.loads(b"".join(chunks))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise _failure(
                "operation-record-tampered", "The operation receipt is malformed."
            ) from exc
        fields = {
            "schema",
            "operation_id",
            "operation",
            "request_digest",
            "phase",
            "identities",
            "result",
            "diagnostic",
            "result_digest",
            "diagnostic_digest",
            "revision",
        }
        if (
            type(value) is not dict
            or set(value) != fields
            or value["schema"] != RECEIPT_SCHEMA
            or value["operation_id"] != operation_id
            or type(value["operation"]) is not str
            or type(value["request_digest"]) is not str
            or DIGEST_RE.fullmatch(value["request_digest"]) is None
            or value["phase"] not in {"intent", "completed", "failed"}
            or type(value["identities"]) is not dict
            or type(value["revision"]) is not int
            or value["revision"] < 0
        ):
            raise _failure(
                "operation-record-tampered", "The operation receipt is malformed."
            )
        if value["phase"] == "completed":
            if (
                type(value["result"]) is not dict
                or value["diagnostic"] is not None
                or value["result_digest"] != canonical_digest(value["result"])
                or value["diagnostic_digest"] is not None
            ):
                raise _failure(
                    "operation-record-tampered", "The operation receipt digest is invalid."
                )
        elif value["phase"] == "failed":
            if (
                type(value["diagnostic"]) is not dict
                or value["result"] is not None
                or value["diagnostic_digest"]
                != canonical_digest(value["diagnostic"])
                or value["result_digest"] is not None
            ):
                raise _failure(
                    "operation-record-tampered", "The operation receipt digest is invalid."
                )
        elif any(
            value[name] is not None
            for name in ("result", "diagnostic", "result_digest", "diagnostic_digest")
        ):
            raise _failure(
                "operation-record-tampered", "The operation intent is malformed."
            )
        return value

    def _write_receipt(self, operation_id: str, value: Mapping[str, Any]) -> None:
        path = self._receipt_path(operation_id)
        normalized = json.loads(canonical_json_bytes(value))
        _atomic_private_json(path, normalized)
        observed = self._read_receipt(operation_id)
        if observed != normalized:
            raise _failure(
                "operation-record-tampered", "The operation receipt was not durable."
            )

    def mutate(
        self,
        request: GatewayRequest,
        handler: Callable[[Mapping[str, Any]], Mapping[str, Any]],
    ) -> dict[str, Any]:
        if request.operation_id is None:
            raise _failure(
                "protocol-incompatible", "A mutating operation requires operation_id."
            )
        payload_identity_field = {
            "allocate_upload": "digest",
            "commit_upload": "digest",
            "publish_deployment": "object_id",
        }.get(request.operation)
        if payload_identity_field is not None and (
            request.payload_digest is None
            or request.arguments.get(payload_identity_field) != request.payload_digest
        ):
            raise _failure(
                "protocol-incompatible",
                "The request payload digest does not match its referenced bytes.",
            )
        existing = self._read_receipt(request.operation_id)
        if existing is not None:
            if (
                existing["operation"] != request.operation
                or existing["request_digest"] != request.operation_digest
            ):
                raise _failure(
                    "operation-conflict",
                    "The operation ID is already bound to different request bytes.",
                    retry_safety="not-applicable",
                    next_action="create-new-operation",
                )
            if existing["phase"] == "completed":
                return dict(existing["result"])
            if existing["phase"] == "failed":
                diagnostic = existing["diagnostic"]
                assert type(diagnostic) is dict
                raise GatewayOperationFailure(
                    diagnostic["category"],
                    diagnostic["message"],
                    phase=diagnostic["phase"],
                    allocation_state=diagnostic["allocation_state"],
                    retry_safety=diagnostic["retry_safety"],
                    next_action=diagnostic["next_action"],
                    identities=diagnostic["identities"],
                )
            raise _failure(
                "submission-uncertain",
                "The operation intent is durable but completion is unknown.",
                allocation_state="unknown",
                retry_safety="same-attempt-only",
                next_action="attach-run-or-retry-same-attempt",
            )
        receipt: dict[str, Any] = {
            "schema": RECEIPT_SCHEMA,
            "operation_id": request.operation_id,
            "operation": request.operation,
            "request_digest": request.operation_digest,
            "phase": "intent",
            "identities": {},
            "result": None,
            "diagnostic": None,
            "result_digest": None,
            "diagnostic_digest": None,
            "revision": 0,
        }
        self._write_receipt(request.operation_id, receipt)
        try:
            raw = handler(request.arguments)
            if not isinstance(raw, Mapping):
                raise TypeError("A gateway handler must return a mapping.")
            result = dict(raw)
        except GatewayOperationFailure as exc:
            receipt.update(
                phase="failed",
                diagnostic=exc.diagnostic,
                diagnostic_digest=canonical_digest(exc.diagnostic),
                revision=1,
            )
            self._write_receipt(request.operation_id, receipt)
            raise
        except Exception as exc:
            failure = _failure(
                "remote-operation-failed",
                "The gateway operation failed without exposing internal details.",
                retry_safety="same-attempt-only",
                next_action="inspect-private-cluster-log",
            )
            receipt.update(
                phase="failed",
                diagnostic=failure.diagnostic,
                diagnostic_digest=canonical_digest(failure.diagnostic),
                revision=1,
            )
            self._write_receipt(request.operation_id, receipt)
            raise failure from exc
        receipt.update(
            phase="completed",
            result=result,
            result_digest=canonical_digest(result),
            revision=1,
        )
        self._write_receipt(request.operation_id, receipt)
        return result


def capabilities() -> dict[str, Any]:
    return {
        "gateway_version": GATEWAY_VERSION,
        "gateway_artifact_digest": os.environ.get(
            "BIOIMAGEFLOW_GATEWAY_ARTIFACT_DIGEST"
        ),
        "gateway_publication_id": os.environ.get(
            "BIOIMAGEFLOW_GATEWAY_PUBLICATION_ID"
        ),
        "supported_protocol_versions": [PROTOCOL_VERSION],
        "request_schema": "bioimageflow.cluster.request.v1",
        "response_schema": "bioimageflow.cluster.response.v1",
        "operation_receipt_schema": RECEIPT_SCHEMA,
        "root_namespaces": list(ROOT_NAMESPACES),
        "operations": [
            "allocate_upload",
            "capabilities",
            "commit_upload",
            "publish_deployment",
            "validate-deployment",
        ],
        "environment_installation_supported": False,
        "existing_python_attestation_supported": True,
        "environment_adapter_versions": {"existing_python": 1},
    }


def _default_handlers(
    state: GatewayState,
) -> dict[str, Callable[[Mapping[str, Any]], Mapping[str, Any]]]:
    return {
        "allocate_upload": state.allocate_upload,
        "commit_upload": state.commit_upload,
        "publish_deployment": state.publish_deployment,
        "validate-deployment": state.validate_deployment,
    }


def handle_request(
    state: GatewayState,
    request: GatewayRequest,
    handlers: Mapping[str, Callable[[Mapping[str, Any]], Mapping[str, Any]]] | None = None,
) -> GatewayResponse:
    """Dispatch one request, journaling any request carrying an operation ID."""
    active_handlers = _default_handlers(state)
    if handlers is not None:
        active_handlers.update(handlers)
    try:
        if request.operation == "capabilities":
            if request.operation_id is not None or request.arguments:
                raise _failure(
                    "protocol-incompatible", "capabilities accepts no arguments."
                )
            result = capabilities()
        else:
            try:
                handler = active_handlers[request.operation]
            except KeyError as exc:
                raise _failure(
                    "protocol-incompatible", "The gateway operation is unsupported."
                ) from exc
            if request.operation_id is None:
                result = dict(handler(request.arguments))
            else:
                result = state.mutate(request, handler)
        return GatewayResponse.ok(request.request_id, result)
    except GatewayOperationFailure as exc:
        return GatewayResponse.error(request.request_id, exc.diagnostic)


def run_gateway(
    state: GatewayState,
    encoded: bytes,
    handlers: Mapping[str, Callable[[Mapping[str, Any]], Mapping[str, Any]]] | None = None,
) -> bytes:
    """Validate and execute one bounded gateway request."""
    try:
        request = GatewayRequest.decode(encoded)
    except GatewayProtocolError:
        # A trustworthy response cannot echo an unvalidated request ID.  Stable
        # entry wrappers should log this locally and return a non-zero status.
        raise
    return handle_request(state, request, handlers).encode()


def main() -> int:
    """Gateway console entry used by an immutable stable dispatcher."""
    root = os.environ.get("BIOIMAGEFLOW_CLUSTER_ROOT")
    if root is None:
        return 2
    try:
        state = GatewayState(root)
        encoded = sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1)
        response = run_gateway(state, encoded)
    except (GatewayProtocolError, GatewayOperationFailure):
        return 2
    sys.stdout.buffer.write(response)
    sys.stdout.buffer.write(b"\n")
    sys.stdout.buffer.flush()
    return 0


__all__ = [
    "RECEIPT_SCHEMA",
    "ROOT_NAMESPACES",
    "GatewayOperationFailure",
    "GatewayState",
    "capabilities",
    "handle_request",
    "run_gateway",
]


if __name__ == "__main__":
    raise SystemExit(main())
