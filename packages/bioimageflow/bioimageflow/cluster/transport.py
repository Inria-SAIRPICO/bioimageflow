"""Shell-free OpenSSH transport for the managed cluster gateway."""

from __future__ import annotations

import hashlib
import math
import os
import re
import shutil
import subprocess
import tempfile
import uuid
import zipfile
from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from typing import Any

from ._common import DIGEST_RE, normalized_cluster_path, validate_host
from .protocol import (
    MAX_RESPONSE_BYTES,
    PROTOCOL_VERSION,
    GatewayProtocolError,
    GatewayRequest,
    GatewayResponse,
)


_ENV_ALLOWLIST = (
    "HOME",
    "LANG",
    "LC_ALL",
    "LOGNAME",
    "PATH",
    "SSH_AUTH_SOCK",
    "USER",
)
_SAFE_REMOTE_OPERAND = re.compile(r"^/[A-Za-z0-9._/+:@-]+$")


class GatewayTransportError(RuntimeError):
    """A sanitized local transport or remote-operation failure."""

    def __init__(
        self,
        category: str,
        message: str,
        *,
        ambiguous: bool = False,
        diagnostic: Mapping[str, Any] | None = None,
    ) -> None:
        self.category = category
        self.ambiguous = ambiguous
        self.diagnostic = None if diagnostic is None else dict(diagnostic)
        super().__init__(message)


def _environment() -> dict[str, str]:
    return {name: os.environ[name] for name in _ENV_ALLOWLIST if name in os.environ}


def _timeout_option(value: float) -> int:
    return max(1, math.ceil(value))


def _diagnostic_category(value: Mapping[str, Any]) -> str:
    category = value.get("category")
    return category if type(category) is str and category else "remote-operation-failed"


def _diagnostic_message(value: Mapping[str, Any]) -> str:
    message = value.get("message")
    if type(message) is str and message:
        return message
    return "The managed cluster operation failed."


class GatewayClientTransport:
    """Invoke the stable gateway entry once for each bounded request.

    The first argument may be a config-like value exposing ``host``, ``root`` and
    optionally ``connect_timeout``.  Explicit arguments are useful to attach with
    only a destination and cluster root.
    """

    def __init__(
        self,
        host: str | Any,
        root: str | PurePosixPath | None = None,
        connect_timeout: float | None = None,
    ) -> None:
        if root is None and not isinstance(host, str):
            config = host
            host = config.host
            root = config.root
            if connect_timeout is None:
                connect_timeout = config.connect_timeout
        if root is None:
            raise TypeError("root is required when host is passed explicitly.")
        self.host = validate_host(host)
        self.root = normalized_cluster_path(root, field="root", safe_token=True)
        timeout = 15.0 if connect_timeout is None else connect_timeout
        if type(timeout) not in {int, float} or not math.isfinite(float(timeout)):
            raise ValueError("connect_timeout must be a positive finite number.")
        self.connect_timeout = float(timeout)
        if not 0 < self.connect_timeout <= 600:
            raise ValueError("connect_timeout must be between 0 and 600 seconds.")
        self._issued_upload_paths: set[str] = set()
        self._gateway_trusted = False
        self._expected_gateway_artifact_digest: str | None = None

    @property
    def gateway_entry(self) -> PurePosixPath:
        return self.root / "gateway" / "entry"

    def _ssh_argv(self) -> list[str]:
        return [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            f"ConnectTimeout={_timeout_option(self.connect_timeout)}",
            "--",
            self.host,
            str(self.gateway_entry),
        ]

    def _run(
        self,
        request: GatewayRequest,
        *,
        timeout: float | None = None,
    ) -> GatewayResponse:
        effective_timeout = self.connect_timeout if timeout is None else timeout
        if type(effective_timeout) not in {int, float} or not math.isfinite(
            float(effective_timeout)
        ) or effective_timeout <= 0:
            raise ValueError("timeout must be a positive finite number.")
        try:
            completed = subprocess.run(
                self._ssh_argv(),
                input=request.encode(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
                timeout=float(effective_timeout),
                env=_environment(),
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise GatewayTransportError(
                "ssh-timeout",
                "The managed cluster request timed out.",
                ambiguous=True,
            ) from exc
        except FileNotFoundError as exc:
            raise GatewayTransportError(
                "ssh-unavailable", "The OpenSSH client is unavailable."
            ) from exc
        except OSError as exc:
            raise GatewayTransportError(
                "ssh-unavailable", "The OpenSSH client could not be started."
            ) from exc
        if completed.returncode != 0:
            raise GatewayTransportError(
                "ssh-unavailable" if completed.returncode == 255 else "gateway-unavailable",
                "The cluster gateway could not be invoked.",
                ambiguous=request.operation_id is not None,
            )
        encoded = completed.stdout
        if len(encoded) > MAX_RESPONSE_BYTES + 1:
            raise GatewayTransportError(
                "protocol-incompatible",
                "The cluster response exceeds its byte limit.",
                ambiguous=request.operation_id is not None,
            )
        if encoded.endswith(b"\n"):
            encoded = encoded[:-1]
        try:
            response = GatewayResponse.decode(encoded)
        except GatewayProtocolError as exc:
            raise GatewayTransportError(
                exc.category,
                "The cluster returned an invalid gateway response.",
                ambiguous=request.operation_id is not None,
            ) from exc
        if response.request_id != request.request_id:
            raise GatewayTransportError(
                "protocol-incompatible",
                "The cluster response does not match the request.",
                ambiguous=request.operation_id is not None,
            )
        if PROTOCOL_VERSION not in response.supported_protocol_versions:
            raise GatewayTransportError(
                "protocol-incompatible",
                "The installed gateway does not support this client protocol.",
                ambiguous=request.operation_id is not None,
            )
        return response

    def request(
        self,
        operation: str,
        arguments: Mapping[str, Any],
        operation_id: str | None = None,
        *,
        payload_digest: str | None = None,
        request_id: str | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Execute an operation and return its validated JSON payload."""
        if operation != "capabilities" and not self._gateway_trusted:
            report = self.check_connection()
            if not report["gateway_available"]:
                raise GatewayTransportError(
                    "gateway-untrusted",
                    "No trusted compatible gateway is installed at the cluster root.",
                )
        request = GatewayRequest.create(
            operation,
            arguments,
            request_id=request_id,
            operation_id=operation_id,
            payload_digest=payload_digest,
        )
        response = self._run(request, timeout=timeout)
        if response.status == "error":
            assert response.diagnostic is not None
            raise GatewayTransportError(
                _diagnostic_category(response.diagnostic),
                _diagnostic_message(response.diagnostic),
                ambiguous=False,
                diagnostic=response.diagnostic,
            )
        assert response.payload is not None
        payload = dict(response.to_dict()["payload"])
        for key in ("upload_path", "partial_path"):
            path = payload.get(key)
            if (
                type(path) is str
                and _SAFE_REMOTE_OPERAND.fullmatch(path) is not None
                and path.startswith(f"{self.root}/")
            ):
                self._issued_upload_paths.add(path)
        return payload

    def check_connection(self) -> dict[str, Any]:
        """Return a ``ClusterConnectionReport``-compatible dictionary.

        A missing stable entry is reported as bootstrap-required.  This method does
        not run setup discovery or write any remote state.
        """
        if self._expected_gateway_artifact_digest is None:
            from .gateway_artifact import build_gateway_artifact

            with build_gateway_artifact() as expected:
                self._expected_gateway_artifact_digest = expected.digest
        try:
            payload = self.request("capabilities", {})
        except GatewayTransportError as exc:
            if exc.category == "gateway-unavailable":
                return {
                    "schema": "bioimageflow.cluster_connection_report.v1",
                    "reachable": True,
                    "gateway_available": False,
                    "bootstrap_required": True,
                    "gateway_version": None,
                    "protocol_versions": [],
                    "diagnostics": [],
                }
            return {
                "schema": "bioimageflow.cluster_connection_report.v1",
                "reachable": False,
                "gateway_available": False,
                "bootstrap_required": False,
                "gateway_version": None,
                "protocol_versions": [],
                "diagnostics": [
                    {
                        "schema": "bioimageflow.cluster_diagnostic.v1",
                        "phase": "connection",
                        "category": exc.category,
                        "message": str(exc),
                        "allocation_state": "none",
                        "retry_safety": "safe",
                        "next_action": "check-ssh-connection",
                        "identities": {},
                    }
                ],
            }
        versions = payload.get("supported_protocol_versions", [PROTOCOL_VERSION])
        version = payload.get("gateway_version")
        if type(versions) is not list or any(type(item) is not int for item in versions):
            raise GatewayTransportError(
                "protocol-incompatible", "Gateway capabilities are invalid."
            )
        if version is not None and type(version) is not str:
            raise GatewayTransportError(
                "protocol-incompatible", "Gateway capabilities are invalid."
            )
        artifact_digest = payload.get("gateway_artifact_digest")
        if type(artifact_digest) is not str or DIGEST_RE.fullmatch(artifact_digest) is None:
            raise GatewayTransportError(
                "gateway-untrusted", "The installed gateway has no trusted artifact identity."
            )
        expected_digest = self._expected_gateway_artifact_digest
        assert expected_digest is not None
        if artifact_digest != expected_digest:
            raise GatewayTransportError(
                "gateway-untrusted",
                "The installed gateway artifact is not in the local compatibility catalog.",
            )
        self._gateway_trusted = True
        return {
            "schema": "bioimageflow.cluster_connection_report.v1",
            "reachable": True,
            "gateway_available": True,
            "bootstrap_required": False,
            "gateway_version": version,
            "protocol_versions": versions,
            "diagnostics": [],
        }

    def upload_file(self, source: Path, remote_path: str) -> None:
        """Transfer one file to an exact path previously issued by the gateway."""
        source = Path(source)
        if (
            remote_path not in self._issued_upload_paths
            or _SAFE_REMOTE_OPERAND.fullmatch(remote_path) is None
        ):
            raise GatewayTransportError(
                "unsafe-upload-target",
                "The upload target was not issued by this gateway session.",
            )
        if source.is_symlink() or not source.is_file():
            raise ValueError("The upload source must be a regular non-symlink file.")
        with tempfile.TemporaryDirectory(prefix="bif-sftp-") as directory:
            safe_source = Path(directory) / "payload"
            shutil.copyfile(source, safe_source, follow_symlinks=False)
            batch = f'put "{safe_source}" "{remote_path}"\n'.encode("ascii")
            argv = [
                "sftp",
                "-b",
                "-",
                "-o",
                "BatchMode=yes",
                "-o",
                f"ConnectTimeout={_timeout_option(self.connect_timeout)}",
                "--",
                self.host,
            ]
            try:
                completed = subprocess.run(
                    argv,
                    input=batch,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    shell=False,
                    timeout=self.connect_timeout,
                    env=_environment(),
                    check=False,
                )
            except subprocess.TimeoutExpired as exc:
                raise GatewayTransportError(
                    "ssh-timeout", "The cluster upload timed out.", ambiguous=True
                ) from exc
            except (FileNotFoundError, OSError) as exc:
                raise GatewayTransportError(
                    "ssh-unavailable", "The SFTP client is unavailable."
                ) from exc
            if completed.returncode != 0:
                raise GatewayTransportError(
                    "ssh-unavailable",
                    "The cluster upload failed.",
                    ambiguous=True,
                )

    def publish_deployment(
        self, prepared: Any, *, progress: Any = None
    ) -> dict[str, Any]:
        """Upload and publish one already snapshotted deployment.

        This helper retains one operation ID across allocation, commit and
        publication so the gateway can resume after a lost acknowledgement.
        """
        prepared.verify()
        from .gateway_artifact import build_gateway_artifact

        artifact = build_gateway_artifact()
        self._expected_gateway_artifact_digest = artifact.digest
        try:
            artifact.verify()
            report = self.check_connection()
            if report["bootstrap_required"]:
                setup_manifest = prepared.manifest.get("setup")
                if (
                    setup_manifest is not None
                    and setup_manifest.get("source_kind") == "cluster_file"
                ):
                    raise GatewayTransportError(
                        "setup-digest-mismatch",
                        "Pinned cluster-resident setup bootstrap is not available in this client.",
                    )
                setup_path = prepared.root / "setup" / "setup.sh"
                setup = setup_path.read_bytes() if setup_path.is_file() else b"true\n"
                from .bootstrap import BootstrapClient

                artifact.verify()
                BootstrapClient(
                    self.host, self.root, self.connect_timeout
                ).install(artifact.path, setup=setup)
                self._gateway_trusted = False
                installed = self.check_connection()
                if not installed["gateway_available"]:
                    raise GatewayTransportError(
                        "gateway-untrusted", "The bootstrapped gateway is unavailable."
                    )
            elif not report["gateway_available"]:
                raise GatewayTransportError(
                    "ssh-unavailable", "The cluster is not reachable for deployment."
                )
        finally:
            artifact.close()
        operation_ids = [str(uuid.uuid4()) for _ in range(3)]
        if progress is not None:
            progress({"phase": "deployment-upload", "state": "preparing"})
        with tempfile.TemporaryDirectory(prefix="bif-deployment-upload-") as directory:
            archive = Path(directory) / "deployment.zip"
            with zipfile.ZipFile(
                archive,
                "x",
                compression=zipfile.ZIP_DEFLATED,
                compresslevel=6,
            ) as output:
                for source in sorted(prepared.root.rglob("*")):
                    if source.is_file():
                        output.write(source, source.relative_to(prepared.root).as_posix())
            prepared.verify()
            size = archive.stat().st_size
            digest = f"sha256:{hashlib.sha256(archive.read_bytes()).hexdigest()}"
            allocation = self.request(
                "allocate_upload",
                {"size": size, "digest": digest, "kind": "deployment"},
                operation_ids[0],
                payload_digest=digest,
            )
            path = allocation.get("upload_path")
            token = allocation.get("upload_token")
            if type(path) is not str or type(token) is not str:
                raise GatewayTransportError(
                    "protocol-incompatible", "Gateway upload allocation is invalid."
                )
            self.upload_file(archive, path)
            if progress is not None:
                progress({"phase": "deployment-upload", "state": "uploaded", "bytes": size})
            committed = self.request(
                "commit_upload",
                {"upload_token": token, "size": size, "digest": digest},
                operation_ids[1],
                payload_digest=digest,
            )
            object_id = committed.get("object_id")
            if type(object_id) is not str or DIGEST_RE.fullmatch(object_id) is None:
                raise GatewayTransportError(
                    "protocol-incompatible", "Gateway object identity is invalid."
                )
            publication = self.request(
                "publish_deployment",
                {
                    "object_id": object_id,
                    "deployment_id": prepared.deployment_id,
                    "manifest_digest": prepared.manifest["manifest_digest"],
                },
                operation_ids[2],
                payload_digest=digest,
            )
            if publication.get("environment_installed") is not True:
                diagnostic = {
                    "schema": "bioimageflow.cluster_diagnostic.v1",
                    "phase": "deployment",
                    "category": "deployment-install-failed",
                    "message": (
                        "Deployment bytes were published, but no runnable environment "
                        "was installed."
                    ),
                    "allocation_state": "none",
                    "retry_safety": "safe",
                    "next_action": "install-deployment-environment",
                    "identities": {"deployment_id": prepared.deployment_id},
                }
                raise GatewayTransportError(
                    "deployment-install-failed",
                    diagnostic["message"],
                    diagnostic=diagnostic,
                )
            if progress is not None:
                progress({"phase": "deployment", "state": "published"})
            return {
                key: publication[key]
                for key in (
                    "deployment_id",
                    "manifest_digest",
                    "reused",
                    "gateway_publication_id",
                    "external_attestation_digest",
                )
                if key in publication
            }

    def submit_plan(self, plan: Any, prepared: Any) -> dict[str, Any]:
        """Allocate, upload, and submit one immutable preplanned invocation."""
        plan._verify_digest()
        prepared._verify()
        if plan.invocation_digest != prepared.invocation_digest:
            raise ValueError("The execution plan belongs to another invocation.")
        with tempfile.TemporaryDirectory(prefix="bif-invocation-upload-") as directory:
            archive = Path(directory) / "invocation.zip"
            with zipfile.ZipFile(
                archive, "x", compression=zipfile.ZIP_DEFLATED, compresslevel=6
            ) as output:
                for source in sorted(prepared.root.rglob("*")):
                    if not source.is_file():
                        continue
                    info = zipfile.ZipInfo(
                        source.relative_to(prepared.root).as_posix(),
                        date_time=(1980, 1, 1, 0, 0, 0),
                    )
                    info.compress_type = zipfile.ZIP_DEFLATED
                    info.external_attr = (0o100600 & 0xFFFF) << 16
                    with source.open("rb") as stream:
                        output.writestr(info, stream.read())
            prepared._verify()
            size = archive.stat().st_size
            digest_value = hashlib.sha256()
            with archive.open("rb") as stream:
                while chunk := stream.read(1024 * 1024):
                    digest_value.update(chunk)
            digest = f"sha256:{digest_value.hexdigest()}"
            arguments = {
                "plan": plan.to_dict(),
                "invocation_manifest": prepared.to_dict(),
                "object_id": digest,
                "object_size": size,
            }
            # The exact archive exists and has been reverified before this first
            # request is allowed to negotiate or contact the cluster.
            result = self.request(
                "submit-plan",
                arguments,
                plan.attempt_id,
                payload_digest=digest,
                timeout=max(self.connect_timeout, 120.0),
            )
            if result.get("upload_required") is True:
                allocation = self.request(
                    "allocate_upload",
                    {"size": size, "digest": digest, "kind": "invocation"},
                    f"{plan.attempt_id}:allocate",
                    payload_digest=digest,
                )
                path = allocation.get("upload_path")
                token = allocation.get("upload_token")
                if type(path) is not str or type(token) is not str:
                    raise GatewayTransportError(
                        "protocol-incompatible", "Gateway upload allocation is invalid."
                    )
                self.upload_file(archive, path)
                committed = self.request(
                    "commit_upload",
                    {"upload_token": token, "size": size, "digest": digest},
                    f"{plan.attempt_id}:commit",
                    payload_digest=digest,
                )
                if committed.get("object_id") != digest:
                    raise GatewayTransportError(
                        "protocol-incompatible", "Gateway invocation identity changed."
                    )
                result = self.request(
                    "submit-plan",
                    arguments,
                    plan.attempt_id,
                    payload_digest=digest,
                    timeout=max(self.connect_timeout, 180.0),
                )
            if result.get("upload_required") is not False:
                raise GatewayTransportError(
                    "submission-uncertain",
                    "The gateway did not durably complete the planned submission.",
                    ambiguous=True,
                )
            return result

    def download_result(self, run_id: str, destination: Path) -> Any:
        """Prepare and atomically materialize one verified portable result."""
        response = self.request(
            "prepare-result",
            {"run_id": run_id},
            str(uuid.uuid4()),
            timeout=max(self.connect_timeout, 120.0),
        )
        from bioimageflow.launcher.result_download import download_result
        from bioimageflow.launcher.types import SSHSubmissionTransport

        legacy_transport = SSHSubmissionTransport(
            host=self.host,
            staging_root=self.root / "transfers" / "runtime",
            remote_executable=self.gateway_entry,
            connect_timeout=self.connect_timeout,
        )
        return download_result(legacy_transport, response, Path(destination))


__all__ = ["GatewayClientTransport", "GatewayTransportError"]
