"""Installed one-shot managed-cluster gateway and durable receipt store."""

from __future__ import annotations

import json
import hashlib
import os
import re
import stat
import sys
import unicodedata
import uuid
import zipfile
from collections.abc import Callable, Mapping
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
) -> None:
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
        _validate_deployment_archive(
            object_path, value["deployment_id"], value["manifest_digest"]
        )
        destination = self.root / "deployments" / value["deployment_id"][7:]
        publication = {
            "schema": "bioimageflow.cluster.deployment_publication.v1",
            "deployment_id": value["deployment_id"],
            "manifest_digest": value["manifest_digest"],
            "object_id": value["object_id"],
            "state": "published",
            "environment_installed": False,
            "external_attestation_digest": None,
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
        candidate = candidates / f"{value['deployment_id'][7:]}.{uuid.uuid4().hex}"
        candidate.mkdir(mode=0o700)
        artifact = candidate / "artifact.zip"
        source_descriptor = os.open(
            object_path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        )
        destination_descriptor = os.open(
            artifact,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        try:
            while chunk := os.read(source_descriptor, 1024 * 1024):
                os.write(destination_descriptor, chunk)
            os.fsync(destination_descriptor)
        finally:
            os.close(source_descriptor)
            os.close(destination_descriptor)
        _validate_published_file(artifact, value["object_id"])
        _atomic_private_json(candidate / "publication.json", publication)
        _fsync_directory(candidate)
        try:
            os.rename(candidate, destination)
        except FileExistsError as exc:
            raise _failure(
                "deployment-tampered", "Deployment publication raced another writer."
            ) from exc
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
        raise _failure(
            "deployment-install-failed",
            "No installed deployment validation adapter is available.",
            phase="validation",
            retry_safety="safe",
            next_action="install-deployment-environment",
            identities={"deployment_id": deployment_id},
        )

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
