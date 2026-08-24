"""Deployment and root-state operations for the managed gateway."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import shlex
import shutil
import stat
import uuid
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from bioimageflow.storage import canonical_json_bytes

from ._common import DIGEST_RE, canonical_digest, normalized_cluster_path
from ._gateway_archive import _supported_scheduler_job
from ._gateway_cleanup_state import GatewayCleanupMixin
from ._gateway_receipts import GatewayReceiptMixin
from ._gateway_run_state import GatewayRunMixin
from ._gateway_scripts import (
    _FACTORY_VALIDATOR_SCRIPT,
    _RUN_CONTROLLER_SCRIPT,
    _RUN_SUBMITTER_SCRIPT,
)
from ._gateway_support import (
    ROOT_NAMESPACES,
    _MAX_UPLOAD_BYTES,
    _UPLOAD_TOKEN_RE,
    _atomic_private_bytes,
    _atomic_private_json,
    _attest_existing_python,
    _child_environment,
    _entry_digest,
    _exact_arguments,
    _extract_deployment_archive,
    _failure,
    _file_digest,
    _fsync_directory,
    _run_json_child,
    _stat_private_directory,
    _validate_deployment_archive,
    _validate_published_file,
    _verify_extracted_deployment,
)
from ._gateway_uv import attest_managed_uv, realize_managed_uv
from .protocol import GATEWAY_VERSION, PROTOCOL_VERSION


class GatewayState(GatewayRunMixin, GatewayCleanupMixin, GatewayReceiptMixin):
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
        cleanup_key = path / "gateway" / "cleanup-plan.key"
        if not cleanup_key.exists() and not cleanup_key.is_symlink():
            descriptor = os.open(
                cleanup_key,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            try:
                key = os.urandom(32)
                offset = 0
                while offset < len(key):
                    offset += os.write(descriptor, key[offset:])
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            _fsync_directory(cleanup_key.parent)
        return cls(path)

    def validate_layout(self) -> None:
        _stat_private_directory(self.root)
        for name in ROOT_NAMESPACES:
            _stat_private_directory(self.root / name)

    def _cleanup_signing_key(self) -> bytes:
        path = self.root / "gateway" / "cleanup-plan.key"
        try:
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        except FileNotFoundError as exc:
            raise _failure(
                "gateway-untrusted",
                "The installed gateway has no cleanup signing identity.",
            ) from exc
        try:
            metadata = os.fstat(descriptor)
            key = os.read(descriptor, 33)
        finally:
            os.close(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) & 0o077
            or metadata.st_nlink != 1
            or len(key) != 32
        ):
            raise _failure("gateway-untrusted", "The cleanup signing identity is unsafe.")
        return key

    def _cleanup_plan_id(self, payload: Mapping[str, Any]) -> str:
        publication_id = os.environ.get("BIOIMAGEFLOW_GATEWAY_PUBLICATION_ID")
        bound = {
            "schema": "bioimageflow.cluster.cleanup_confirmation.v1",
            "root": str(self.root),
            "gateway_publication_id": publication_id,
            "plan": dict(payload),
        }
        digest = hmac.new(
            self._cleanup_signing_key(), canonical_json_bytes(bound), hashlib.sha256
        ).hexdigest()
        return f"sha256:{digest}"

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
        if (
            type(environment) is dict
            and environment.get("kind") == "uv"
            and isinstance(manifest.get("environment_plan"), Mapping)
        ):
            return self._publish_managed_uv(
                value, object_path, manifest, prepared_deployment_id
            )
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
            "run_submitter_digest": (
                f"sha256:{hashlib.sha256(_RUN_SUBMITTER_SCRIPT.encode()).hexdigest()}"
                if external
                else None
            ),
            "run_controller_digest": (
                f"sha256:{hashlib.sha256(_RUN_CONTROLLER_SCRIPT.encode()).hexdigest()}"
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
                _atomic_private_bytes(
                    candidate / "submit_run.py", _RUN_SUBMITTER_SCRIPT.encode(), 0o600
                )
                _atomic_private_bytes(
                    candidate / "control_run.py", _RUN_CONTROLLER_SCRIPT.encode(), 0o600
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

    def _publish_managed_uv(
        self,
        value: Mapping[str, Any],
        object_path: Path,
        manifest: Mapping[str, Any],
        prepared_deployment_id: str,
    ) -> dict[str, Any]:
        candidates = self._private_subdirectory(self.root / "temporary", "deployments")
        candidate = candidates / f"uv.{prepared_deployment_id[7:]}.{uuid.uuid4().hex}"
        candidate.mkdir(mode=0o700)
        try:
            artifact = candidate / "artifact.zip"
            shutil.copyfile(object_path, artifact)
            os.chmod(artifact, 0o600, follow_symlinks=False)
            _validate_published_file(artifact, value["object_id"])
            content = candidate / "content"
            _extract_deployment_archive(artifact, content)
            _verify_extracted_deployment(content, manifest)
            attestation, attestation_digest, inventory_digest = realize_managed_uv(
                candidate, content, manifest
            )
            deployment_id = canonical_digest(
                {
                    "schema": "bioimageflow.cluster.managed_uv_deployment_identity.v1",
                    "prepared_deployment_id": prepared_deployment_id,
                    "environment_attestation_digest": attestation_digest,
                    "environment_inventory_digest": inventory_digest,
                }
            )
            destination = self.root / "deployments" / deployment_id[7:]
            python = destination / "environment" / (
                "Scripts/python.exe" if os.name == "nt" else "bin/python"
            )
            activation = (
                "set -eu\n"
                f"export VIRTUAL_ENV={shlex.quote(str(destination / 'environment'))}\n"
                f"export PATH={shlex.quote(str(python.parent))}:\"$PATH\"\n"
                "unset PYTHONHOME\n"
            ).encode()
            publication = {
                "schema": "bioimageflow.cluster.deployment_publication.v1",
                "deployment_id": deployment_id,
                "prepared_deployment_id": prepared_deployment_id,
                "manifest_digest": value["manifest_digest"],
                "object_id": value["object_id"],
                "state": "published",
                "environment_installed": True,
                "environment_kind": "uv",
                "environment_attestation_digest": attestation_digest,
                "environment_attestation": attestation,
                "environment_inventory_digest": inventory_digest,
                "external_attestation_digest": None,
                "external_attestation": None,
                "activation_digest": f"sha256:{hashlib.sha256(activation).hexdigest()}",
                "factory_validator_digest": f"sha256:{hashlib.sha256(_FACTORY_VALIDATOR_SCRIPT.encode()).hexdigest()}",
                "run_submitter_digest": f"sha256:{hashlib.sha256(_RUN_SUBMITTER_SCRIPT.encode()).hexdigest()}",
                "run_controller_digest": f"sha256:{hashlib.sha256(_RUN_CONTROLLER_SCRIPT.encode()).hexdigest()}",
            }
            _atomic_private_bytes(candidate / "activation.sh", activation, 0o600)
            _atomic_private_bytes(
                candidate / "validate_factory.py", _FACTORY_VALIDATOR_SCRIPT.encode(), 0o600
            )
            _atomic_private_bytes(
                candidate / "submit_run.py", _RUN_SUBMITTER_SCRIPT.encode(), 0o600
            )
            _atomic_private_bytes(
                candidate / "control_run.py", _RUN_CONTROLLER_SCRIPT.encode(), 0o600
            )
            _atomic_private_json(candidate / "publication.json", publication)
            _fsync_directory(candidate)
            try:
                os.rename(candidate, destination)
                reused = False
            except FileExistsError:
                shutil.rmtree(candidate, ignore_errors=True)
                _stat_private_directory(destination)
                try:
                    observed = json.loads((destination / "publication.json").read_bytes())
                except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise _failure(
                        "deployment-tampered", "The deployment publication is malformed."
                    ) from exc
                _validate_published_file(
                    destination / "artifact.zip", value["object_id"]
                )
                if observed != publication:
                    raise _failure(
                        "deployment-tampered",
                        "A concurrent managed uv publication has different bytes.",
                    )
                reused = True
            _fsync_directory(destination.parent)
            return {
                **publication,
                "reused": reused,
                "gateway_publication_id": "gateway-v1",
            }
        except BaseException:
            shutil.rmtree(candidate, ignore_errors=True)
            raise

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
        if publication.get("environment_kind") == "uv":
            fresh_attestation, fresh_digest, runtime_python = attest_managed_uv(
                destination,
                manifest,
                publication,
                failure_category="environment-platform-incompatible",
            )
            expected_attestation_digest = publication.get(
                "environment_attestation_digest"
            )
        else:
            fresh_attestation, fresh_digest = _attest_existing_python(
                manifest, failure_category="external-environment-changed"
            )
            runtime_python = fresh_attestation["requested_executable"]
            expected_attestation_digest = publication.get(
                "external_attestation_digest"
            )
        if fresh_digest != expected_attestation_digest:
            changed_category = (
                "environment-platform-incompatible"
                if publication.get("environment_kind") == "uv"
                else "external-environment-changed"
            )
            raise _failure(
                changed_category,
                "The selected Python environment changed after deployment confirmation.",
                phase="validation",
                retry_safety="safe",
                next_action="deploy-and-confirm-again",
                identities={"deployment_id": deployment_id},
            )
        _supported_scheduler_job(
            value["scheduler_job"], expected_scheduler=manifest.get("scheduler")
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
                "environment_name": (
                    "uv" if publication.get("environment_kind") == "uv" else "existing-python"
                ),
                "scheduler": manifest["scheduler"],
                "parsl": parsl,
                "timeout": float(timeout),
            }
            request_path = private / "request.json"
            _atomic_private_json(request_path, request)
            encoded_secrets = canonical_json_bytes(secrets)
            report = _run_json_child(
                [
                    runtime_python,
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
                "login-node Python environment identity",
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
                "external_attestation": (
                    fresh_attestation
                    if publication.get("environment_kind") != "uv"
                    else None
                ),
                "external_attestation_digest": (
                    fresh_digest
                    if publication.get("environment_kind") != "uv"
                    else None
                ),
                "environment_attestation": fresh_attestation,
                "environment_attestation_digest": fresh_digest,
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
        validation_root = self._private_subdirectory(destination, "validations")
        validation_path = validation_root / f"{payload['validation_digest'][7:]}.json"
        if validation_path.exists():
            try:
                existing = json.loads(validation_path.read_bytes())
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise _failure(
                    "deployment-tampered", "The retained validation report is malformed."
                ) from exc
            if existing != payload:
                raise _failure(
                    "deployment-tampered", "The retained validation identity conflicts."
                )
        else:
            _atomic_private_json(validation_path, payload)
        return payload



__all__ = ["GatewayState"]
