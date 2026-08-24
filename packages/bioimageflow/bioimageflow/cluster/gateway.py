"""Installed one-shot managed-cluster gateway and durable receipt store."""

from __future__ import annotations

import json
import hashlib
import os
import re
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import unicodedata
import uuid
import zipfile
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from bioimageflow.storage import canonical_json_bytes

from ._common import DIGEST_RE, canonical_digest, normalized_cluster_path, thaw_json
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
_MAX_CHILD_RESPONSE_BYTES = MAX_REQUEST_BYTES

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

_RUN_SUBMITTER_SCRIPT = r'''from __future__ import annotations
import json
import sys
from pathlib import Path


def main():
    request = json.loads(Path(sys.argv[1]).read_bytes())
    content = Path(request["deployment_content"])
    parsl_root = content / "parsl"
    sys.path.insert(0, str(parsl_root))
    include = parsl_root / "include"
    if include.is_dir():
        for child in sorted(include.iterdir()):
            if child.is_dir():
                sys.path.insert(0, str(child))

    import bioimageflow.launcher.submission as submission_module
    from bioimageflow.launcher.cluster_submit import _load_inputs, _load_node_input_overrides
    from bioimageflow.launcher.payload import load_workflow_payload
    from bioimageflow.launcher.pre_launch import PreLaunchScript
    from bioimageflow.launcher.remote_control import inspect_run
    from bioimageflow.launcher.submission import _submit_workflow
    from bioimageflow.launcher.types import ParslConfigRef, PSIJLaunchConfig
    from bioimageflow.parsl import ExecutorBinding, ParslTaskPolicy

    # Secrets are resolved by the gateway into the run-private pre-launch
    # handoff. They must not enter this submitter process environment.
    submission_module.verify_secret_references = lambda _reference: None
    invocation_root = Path(request["invocation_root"])
    invocation = json.loads((invocation_root / "invocation.json").read_bytes())
    workflow = load_workflow_payload(
        invocation["workflow"], storage_path=Path(request["storage_path"])
    )
    _load_node_input_overrides(
        invocation_root, workflow, invocation["node_input_overrides"]
    )
    inputs = _load_inputs(invocation_root, workflow, invocation["inputs"])
    run = _submit_workflow(
        workflow,
        inputs=inputs if invocation["targets"] is None else None,
        targets=invocation["targets"],
        parsl_config=ParslConfigRef.from_dict(request["parsl_config"]),
        executor_bindings={
            label: ExecutorBinding.from_dict(value)
            for label, value in request["executor_bindings"].items()
        },
        node_routes=request["node_routes"],
        environment_routes={},
        shared_runtime_root=Path(request["shared_runtime_root"]),
        task_policy=ParslTaskPolicy.from_dict(invocation["task_policy"]),
        launch=PSIJLaunchConfig.from_dict(request["launch"]),
        pre_launch=PreLaunchScript.from_text(request["pre_launch"]),
        preallocated_run_id=request["run_id"],
        preserve_cluster_paths=True,
    )
    print(json.dumps({
        "status": "ok",
        "payload": inspect_run(request["storage_path"], run.id),
    }, sort_keys=True, separators=(",", ":")))


if __name__ == "__main__":
    main()
'''

_RUN_CONTROLLER_SCRIPT = r'''from __future__ import annotations
import json
import sys
from pathlib import Path


def main():
    request = json.loads(Path(sys.argv[1]).read_bytes())
    from bioimageflow.launcher.cluster_protocol import ClusterProtocolFailure
    from bioimageflow.launcher.remote_control import (
        cancel_run, inspect_run, plan_run_retry, read_progress_page,
        refresh_run, start_run_retry,
    )
    from bioimageflow.launcher.result_bundle import prepare_result
    operation = request["operation"]
    arguments = request["arguments"]
    storage = request["storage_path"]
    run_id = request["run_id"]
    try:
        if operation == "inspect-run":
            payload = inspect_run(storage, run_id)
        elif operation == "refresh-run":
            payload = refresh_run(storage, run_id)
        elif operation == "read-progress":
            payload = read_progress_page(
                storage, run_id, arguments["after_sequence"], arguments["limit"]
            )
        elif operation == "cancel-run":
            payload = cancel_run(
                request["transfer_root"], storage, run_id,
                request["request_id"], request["request_digest"],
            )
        elif operation == "plan-retry":
            payload = plan_run_retry(storage, run_id, arguments["recompute"])
        elif operation == "start-retry":
            payload = start_run_retry(storage, arguments["plan"])
        elif operation == "prepare-result":
            payload = prepare_result(
                request["transfer_root"], storage, run_id,
                request["request_id"], request["request_digest"],
            )
        else:
            raise RuntimeError("unsupported managed run operation")
    except ClusterProtocolFailure as exc:
        print(json.dumps({"status": "error", "category": exc.code, "message": exc.message}, sort_keys=True, separators=(",", ":")))
        return
    print(json.dumps({"status": "ok", "payload": payload}, sort_keys=True, separators=(",", ":")))


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


def _canonical_run_id(value: Any) -> str:
    try:
        parsed = uuid.UUID(value, version=4)
    except (AttributeError, TypeError, ValueError):
        try:
            from bioimageflow.launcher.schemas import validate_run_id

            return validate_run_id(value)
        except (TypeError, ValueError) as exc:
            raise _failure("protocol-incompatible", "run_id is invalid.") from exc
    if str(parsed) != value:
        raise _failure("protocol-incompatible", "run_id is invalid.")
    return value


def _canonical_attempt_id(value: Any) -> str:
    try:
        parsed = uuid.UUID(value, version=4)
    except (AttributeError, TypeError, ValueError) as exc:
        raise _failure("protocol-incompatible", "attempt_id is invalid.") from exc
    if str(parsed) != value:
        raise _failure("protocol-incompatible", "attempt_id is invalid.")
    return value


def _invocation_manifest(value: Any, *, expected_digest: str) -> dict[str, Any]:
    try:
        from .preparation import PreparedInvocationManifest

        parsed = PreparedInvocationManifest.from_dict(value)
    except (TypeError, ValueError) as exc:
        raise _failure(
            "protocol-incompatible", "The prepared invocation manifest is invalid."
        ) from exc
    if parsed.invocation_digest != expected_digest:
        raise _failure(
            "operation-conflict", "The execution plan names another invocation."
        )
    return parsed.to_dict()


def _validate_invocation_archive(
    path: Path, manifest: Mapping[str, Any], expected_object_id: str
) -> None:
    _validate_published_file(path, expected_object_id)
    entries = manifest.get("entries")
    if type(entries) is not list:
        raise _failure("deployment-tampered", "The invocation inventory is invalid.")
    declared = {entry.get("path"): entry for entry in entries if type(entry) is dict}
    if len(declared) != len(entries) or "invocation.json" not in declared:
        raise _failure("deployment-tampered", "The invocation inventory is invalid.")
    try:
        archive = zipfile.ZipFile(path)
    except (OSError, zipfile.BadZipFile) as exc:
        raise _failure("deployment-tampered", "The invocation archive is invalid.") from exc
    with archive:
        members = archive.infolist()
        if len(members) > _MAX_ARCHIVE_ENTRIES:
            raise _failure("resource-limit-exceeded", "The invocation has too many entries.")
        files: dict[str, zipfile.ZipInfo] = {}
        total = 0
        for member in members:
            name = member.filename
            relative = PurePosixPath(name)
            unix_type = (member.external_attr >> 16) & 0o170000
            if (
                not name
                or name != unicodedata.normalize("NFC", name)
                or relative.is_absolute()
                or "\\" in name
                or any(part in {"", ".", ".."} for part in relative.parts)
                or name in files
                or member.is_dir()
                or member.flag_bits & 0x1
                or unix_type not in {0, stat.S_IFREG}
            ):
                raise _failure("deployment-tampered", "The invocation archive is unsafe.")
            files[name] = member
            total += member.file_size
            if total > _MAX_ARCHIVE_EXPANDED_BYTES:
                raise _failure("resource-limit-exceeded", "The invocation expands too large.")
        declared_files = {
            path: entry for path, entry in declared.items() if entry.get("kind") == "file"
        }
        if set(files) != set(declared_files):
            raise _failure("deployment-tampered", "The invocation archive inventory changed.")
        for name, member in files.items():
            entry = declared_files[name]
            digest = hashlib.sha256()
            size = 0
            with archive.open(member) as stream:
                while chunk := stream.read(1024 * 1024):
                    size += len(chunk)
                    digest.update(chunk)
            if (
                size != entry.get("size")
                or f"sha256:{digest.hexdigest()}" != entry.get("digest")
            ):
                raise _failure("deployment-tampered", "An invocation entry changed.")


def _extract_invocation_archive(
    path: Path, destination: Path, manifest: Mapping[str, Any]
) -> None:
    destination.mkdir(mode=0o700)
    directories = [
        item["path"]
        for item in manifest["entries"]
        if item.get("kind") == "directory"
    ]
    for name in sorted(directories, key=lambda item: len(PurePosixPath(item).parts)):
        destination.joinpath(*PurePosixPath(name).parts).mkdir(
            mode=0o700, parents=True, exist_ok=True
        )
    with zipfile.ZipFile(path) as archive:
        for member in archive.infolist():
            target = destination.joinpath(*PurePosixPath(member.filename).parts)
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            descriptor = os.open(
                target,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
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

    def _run_directory(self, run_id: Any) -> Path:
        return self.root / "runs" / _canonical_run_id(run_id)

    def _read_run_record(self, run_id: Any) -> dict[str, Any]:
        canonical = _canonical_run_id(run_id)
        path = self._run_directory(canonical) / "record.json"
        try:
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        except FileNotFoundError as exc:
            raise _failure(
                "run-not-found", "The requested managed run does not exist.",
                phase="run-observation", retry_safety="not-applicable",
                next_action="check-run-id", identities={"run_id": canonical},
            ) from exc
        try:
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != os.geteuid()
                or stat.S_IMODE(metadata.st_mode) & 0o077
                or metadata.st_nlink != 1
                or metadata.st_size > MAX_REQUEST_BYTES
            ):
                raise _failure("operation-record-tampered", "The run index is unsafe.")
            encoded = b""
            while chunk := os.read(descriptor, 64 * 1024):
                encoded += chunk
        finally:
            os.close(descriptor)
        try:
            value = json.loads(encoded)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise _failure("operation-record-tampered", "The run index is malformed.") from exc
        fields = {
            "schema", "run_id", "attempt_id", "request_digest", "plan_digest",
            "deployment_id", "invocation_digest", "validation_digest", "object_id",
            "object_size", "storage_path", "phase", "launcher_bound", "plan",
            "invocation_manifest", "revision", "updated_at",
        }
        if (
            type(value) is not dict
            or set(value) != fields
            or value["schema"] != _RUN_RECORD_SCHEMA
            or value["run_id"] != canonical
            or value["phase"] not in _RUN_PHASES
            or type(value["launcher_bound"]) is not bool
            or type(value["revision"]) is not int
            or value["revision"] < 0
            or any(
                type(value[name]) is not str or DIGEST_RE.fullmatch(value[name]) is None
                for name in (
                    "request_digest", "plan_digest", "deployment_id",
                    "invocation_digest", "validation_digest", "object_id",
                )
            )
            or type(value["object_size"]) is not int
            or not 0 <= value["object_size"] <= _MAX_UPLOAD_BYTES
        ):
            raise _failure("operation-record-tampered", "The run index is malformed.")
        _canonical_attempt_id(value["attempt_id"])
        if canonical_digest({key: item for key, item in value["plan"].items() if key != "plan_digest"}) != value["plan_digest"]:
            raise _failure("operation-record-tampered", "The retained plan digest is invalid.")
        return value

    def _write_run_record(self, record: Mapping[str, Any]) -> None:
        run_id = _canonical_run_id(record.get("run_id"))
        path = self._run_directory(run_id) / "record.json"
        _atomic_private_json(path, record)
        if self._read_run_record(run_id) != dict(record):
            raise _failure("operation-record-tampered", "The run index was not durable.")

    @staticmethod
    def _allocated_observation(record: Mapping[str, Any]) -> dict[str, Any]:
        state = "cancelled" if record["phase"] == "cancelled" else "prepared"
        return {
            "schema": "bioimageflow.launcher.run-observation.v1",
            "error": None,
            "retry_plan": None,
            "run_id": record["run_id"],
            "state": state,
            "status_revision": record["revision"],
            "storage_path": record["storage_path"],
            "terminal": state == "cancelled",
            "updated_at": record["updated_at"],
        }

    def _deployment_runtime(
        self, record: Mapping[str, Any], *, require_validation: bool = False
    ) -> tuple[Path, dict[str, Any], dict[str, Any], dict[str, Any] | None]:
        deployment_id = record["deployment_id"]
        destination = self.root / "deployments" / deployment_id[7:]
        _stat_private_directory(destination)
        try:
            publication = json.loads((destination / "publication.json").read_bytes())
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise _failure("deployment-tampered", "The deployment publication is malformed.") from exc
        if (
            publication.get("deployment_id") != deployment_id
            or publication.get("environment_installed") is not True
        ):
            raise _failure("deployment-tampered", "The retained deployment is unavailable.")
        artifact = destination / "artifact.zip"
        _validate_published_file(artifact, publication["object_id"])
        manifest = _validate_deployment_archive(
            artifact,
            publication["prepared_deployment_id"],
            publication["manifest_digest"],
        )
        _verify_extracted_deployment(destination / "content", manifest)
        for name, field in (
            ("activation.sh", "activation_digest"),
            ("submit_run.py", "run_submitter_digest"),
            ("control_run.py", "run_controller_digest"),
        ):
            _validate_published_file(destination / name, publication[field])
        attestation, digest = _attest_existing_python(
            manifest, failure_category="external-environment-changed"
        )
        if digest != publication.get("external_attestation_digest"):
            raise _failure(
                "external-environment-changed",
                "The external Python changed after deployment confirmation.",
                phase="submission", retry_safety="safe",
                next_action="deploy-and-confirm-again",
                identities={"deployment_id": deployment_id},
            )
        validation: dict[str, Any] | None = None
        if require_validation:
            validation_path = destination / "validations" / f"{record['validation_digest'][7:]}.json"
            try:
                loaded_validation = json.loads(validation_path.read_bytes())
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise _failure("validation-expired", "The retained validation is unavailable.") from exc
            if type(loaded_validation) is not dict:
                raise _failure("deployment-tampered", "The retained validation changed.")
            validation = loaded_validation
            if (
                validation.get("validation_digest") != record["validation_digest"]
                or validation.get("deployment_id") != deployment_id
                or validation.get("valid") is not True
                or canonical_digest({key: item for key, item in validation.items() if key != "validation_digest"}) != record["validation_digest"]
            ):
                raise _failure("deployment-tampered", "The retained validation changed.")
            try:
                expires = datetime.fromisoformat(validation["expires_at"].replace("Z", "+00:00"))
            except (AttributeError, TypeError, ValueError) as exc:
                raise _failure("deployment-tampered", "The retained validation expiry is invalid.") from exc
            if datetime.now(timezone.utc) >= expires:
                raise _failure(
                    "validation-expired", "The retained validation report expired.",
                    phase="submission", retry_safety="safe",
                    next_action="validate-and-plan-again",
                )
        return destination, manifest, attestation, validation

    def _run_controller(
        self,
        record: Mapping[str, Any],
        operation: str,
        arguments: Mapping[str, Any],
    ) -> dict[str, Any]:
        deployment, _manifest, attestation, _validation = self._deployment_runtime(record)
        requests = self._private_subdirectory(self.root / "temporary", "run-requests")
        private = requests / uuid.uuid4().hex
        private.mkdir(mode=0o700)
        try:
            request = {
                "operation": operation,
                "arguments": dict(arguments),
                "storage_path": record["storage_path"],
                "run_id": record["run_id"],
                "transfer_root": str(self._private_subdirectory(self.root / "transfers", "runtime")),
                "request_id": str(uuid.uuid4()),
                "request_digest": canonical_digest({"operation": operation, "arguments": dict(arguments)}),
            }
            path = private / "request.json"
            _atomic_private_json(path, request)
            response = _run_json_child(
                [attestation["requested_executable"], "-I", "-B", str(deployment / "control_run.py"), str(path)],
                environment=_child_environment(),
                timeout=60.0,
                output_limit=MAX_REQUEST_BYTES,
                failure_category="remote-operation-failed",
            )
        finally:
            shutil.rmtree(private, ignore_errors=True)
        if response.get("status") == "error":
            raise _failure(
                str(response.get("category", "remote-operation-failed")),
                str(response.get("message", "The managed run operation failed.")),
                phase=operation, retry_safety="safe", next_action="inspect-run",
                identities={"run_id": record["run_id"]},
            )
        if set(response) != {"status", "payload"} or response["status"] != "ok" or type(response["payload"]) is not dict:
            raise _failure("remote-operation-failed", "The managed run response is malformed.")
        return response["payload"]

    def submit_plan_request(self, request: GatewayRequest) -> dict[str, Any]:
        if request.operation_id is None or request.payload_digest is None:
            raise _failure("protocol-incompatible", "submit-plan requires stable attempt and payload identities.")
        value = _exact_arguments(
            request.arguments, {"plan", "invocation_manifest", "object_id", "object_size"}
        )
        try:
            from .plan import RemoteExecutionPlan

            plan = RemoteExecutionPlan.from_dict(value["plan"])
        except (TypeError, ValueError) as exc:
            raise _failure("protocol-incompatible", "The remote execution plan is invalid.") from exc
        if (
            plan.attempt_id != request.operation_id
            or plan.cluster_root != str(self.root)
            or value["object_id"] != request.payload_digest
            or type(value["object_size"]) is not int
            or not 0 <= value["object_size"] <= _MAX_UPLOAD_BYTES
            or type(value["object_id"]) is not str
            or DIGEST_RE.fullmatch(value["object_id"]) is None
        ):
            raise _failure("operation-conflict", "The submit attempt bindings do not match.")
        invocation = _invocation_manifest(
            value["invocation_manifest"], expected_digest=plan.invocation_digest
        )
        run_dir = self._run_directory(plan.run_id)
        if not run_dir.exists():
            try:
                run_dir.mkdir(mode=0o700)
            except FileExistsError:
                pass
        _stat_private_directory(run_dir)
        record_path = run_dir / "record.json"
        if record_path.exists():
            record = self._read_run_record(plan.run_id)
            if record["request_digest"] != request.operation_digest:
                raise _failure("operation-conflict", "The run ID is bound to another plan.")
        else:
            record = {
                "schema": _RUN_RECORD_SCHEMA,
                "run_id": plan.run_id,
                "attempt_id": plan.attempt_id,
                "request_digest": request.operation_digest,
                "plan_digest": plan.plan_digest,
                "deployment_id": plan.deployment_id,
                "invocation_digest": plan.invocation_digest,
                "validation_digest": plan.validation_digest,
                "object_id": value["object_id"],
                "object_size": value["object_size"],
                "storage_path": plan.storage_path,
                "phase": "allocated",
                "launcher_bound": False,
                "plan": plan.to_dict(),
                "invocation_manifest": invocation,
                "revision": 0,
                "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
            }
            self._write_run_record(record)
        if record["phase"] in {"submitted", "cancelled"}:
            observation = (
                self._run_controller(record, "inspect-run", {})
                if record["launcher_bound"]
                else self._allocated_observation(record)
            )
            return {"run_id": plan.run_id, "observation": observation, "upload_required": False}
        if record["phase"] in {"scheduler-intent", "uncertain"}:
            raise _failure(
                "submission-uncertain", "Scheduler acceptance cannot be disproved; the attempt was not resubmitted.",
                phase="submission", allocation_state="unknown", retry_safety="same-attempt-only",
                next_action="attach-run", identities={"run_id": plan.run_id, "attempt_id": plan.attempt_id},
            )
        object_path = self.root / "objects" / f"{record['object_id'][7:]}.object"
        if not object_path.exists():
            if record["phase"] != "uploading":
                record = {**record, "phase": "uploading", "revision": record["revision"] + 1, "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")}
                self._write_run_record(record)
            return {"run_id": plan.run_id, "observation": self._allocated_observation(record), "upload_required": True}
        _validate_invocation_archive(object_path, invocation, record["object_id"])
        invocation_root = run_dir / "invocation"
        if not invocation_root.exists():
            _extract_invocation_archive(object_path, invocation_root, invocation)
        if record["phase"] != "ready":
            record = {**record, "phase": "ready", "revision": record["revision"] + 1, "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")}
            self._write_run_record(record)
        deployment, manifest, attestation, validation = self._deployment_runtime(record, require_validation=True)
        assert validation is not None
        plan_value = plan.to_dict()
        if (
            plan.validation_expires_at != validation["expires_at"]
            or plan_value["validation_evidence"] != validation["evidence"]
            or plan_value["executor_claims"] != validation["executor_bindings"]
        ):
            raise _failure(
                "parsl-configuration-changed",
                "The retained validation claims no longer match the execution plan.",
                phase="submission", retry_safety="safe",
                next_action="validate-and-plan-again",
            )
        scheduler = plan.scheduler_job
        if scheduler.gpu or scheduler.memory is not None or scheduler.attributes:
            raise _failure(
                "unsupported-scheduler-adapter", "The current PSI/J bridge cannot represent GPU, memory, or custom scheduler attributes.",
                phase="submission", retry_safety="safe", next_action="simplify-orchestrator-job",
            )
        parsl = manifest["parsl"]
        factory = (
            f"factory:{parsl['factory']}"
            if parsl["source_kind"] == "file"
            else parsl["source"]
        )
        handoff = run_dir / "secret-handoff.sh"
        secret_lines = ["set -eu"]
        total = 0
        for reference in parsl["secret_refs"].values():
            if reference not in os.environ:
                raise _failure(
                    "secret-reference-missing", f"Required secret reference {reference!r} is unavailable.",
                    phase="submission", retry_safety="safe", next_action="provide-secret-reference",
                    identities={"run_id": plan.run_id},
                )
            secret = os.environ[reference]
            size = len(secret.encode("utf-8"))
            total += size
            if "\0" in secret or size > 64 * 1024 or total > 256 * 1024:
                raise _failure("resource-limit-exceeded", "Resolved submission secrets exceed their handoff limit.")
            secret_lines.append(f"export {reference}={shlex.quote(secret)}")
        if len(secret_lines) > 1 and not handoff.exists():
            _atomic_private_bytes(handoff, ("\n".join(secret_lines) + "\n").encode(), 0o600)
        python_paths = [str(deployment / "content" / "parsl")]
        include = deployment / "content" / "parsl" / "include"
        if include.is_dir():
            python_paths.extend(str(child) for child in sorted(include.iterdir()) if child.is_dir())
        pre_launch = [
            "set -eu",
            f". {shlex.quote(str(deployment / 'activation.sh'))}",
            f"export PYTHONPATH={shlex.quote(':'.join(python_paths))}${{PYTHONPATH:+:$PYTHONPATH}}",
        ]
        setup = deployment / "content" / "setup" / "setup.sh"
        if setup.is_file():
            pre_launch.append(f". {shlex.quote(str(setup))}")
        if len(secret_lines) > 1:
            pre_launch.extend([f". {shlex.quote(str(handoff))}", f"rm -f -- {shlex.quote(str(handoff))}"])
        node_routes = {
            item["node"]: item["selected_executor"]
            for item in plan_value["nodes"]
            if item["kind"] == "processing" and item["will_dispatch"]
        }
        runtime_root = run_dir / "runtime"
        runtime_root.mkdir(mode=0o700, exist_ok=True)
        child_request = {
            "run_id": plan.run_id,
            "storage_path": plan.storage_path,
            "invocation_root": str(invocation_root),
            "deployment_content": str(deployment / "content"),
            "shared_runtime_root": str(runtime_root),
            "parsl_config": {"factory": factory, "kwargs": parsl["kwargs"], "secret_refs": parsl["secret_refs"]},
            "executor_bindings": validation["executor_bindings"],
            "node_routes": node_routes,
            "launch": {
                "backend": "psij", "executor": scheduler.scheduler,
                "walltime_seconds": scheduler.walltime_seconds,
                "queue": scheduler.queue, "project": scheduler.project,
                "cpu_cores": scheduler.cpu, "work_dir": str(run_dir),
                "hard_cancel_after": scheduler.hard_cancel_after,
            },
            "pre_launch": "\n".join(pre_launch) + "\n",
        }
        record = {**record, "phase": "scheduler-intent", "revision": record["revision"] + 1, "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")}
        self._write_run_record(record)
        requests = self._private_subdirectory(self.root / "temporary", "run-requests")
        private = requests / uuid.uuid4().hex
        private.mkdir(mode=0o700)
        try:
            child_path = private / "request.json"
            _atomic_private_json(child_path, child_request)
            try:
                response = _run_json_child(
                    [attestation["requested_executable"], "-I", "-B", str(deployment / "submit_run.py"), str(child_path)],
                    environment=_child_environment(), timeout=120.0,
                    output_limit=MAX_REQUEST_BYTES,
                    failure_category="remote-operation-failed",
                )
            except GatewayOperationFailure:
                record = {**record, "phase": "uncertain", "revision": record["revision"] + 1, "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")}
                self._write_run_record(record)
                raise _failure(
                    "submission-uncertain", "Scheduler submission may have occurred; the attempt was not resubmitted.",
                    phase="submission", allocation_state="unknown", retry_safety="same-attempt-only",
                    next_action="attach-run", identities={"run_id": plan.run_id, "attempt_id": plan.attempt_id},
                )
        finally:
            shutil.rmtree(private, ignore_errors=True)
        if response.get("status") != "ok" or type(response.get("payload")) is not dict:
            raise _failure("remote-operation-failed", "The submitter response is malformed.")
        record = {**record, "phase": "submitted", "launcher_bound": True, "revision": record["revision"] + 1, "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")}
        self._write_run_record(record)
        return {"run_id": plan.run_id, "observation": response["payload"], "upload_required": False}

    def inspect_run(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        value = _exact_arguments(arguments, {"run_id"})
        record = self._read_run_record(value["run_id"])
        return self._run_controller(record, "inspect-run", {}) if record["launcher_bound"] else self._allocated_observation(record)

    def refresh_run(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        value = _exact_arguments(arguments, {"run_id"})
        record = self._read_run_record(value["run_id"])
        return self._run_controller(record, "refresh-run", {}) if record["launcher_bound"] else self._allocated_observation(record)

    def read_progress(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        value = _exact_arguments(arguments, {"run_id", "after_sequence", "limit"})
        record = self._read_run_record(value["run_id"])
        if not record["launcher_bound"]:
            return {**self._allocated_observation(record), "events": [], "has_more": False, "next_sequence": value["after_sequence"]}
        return self._run_controller(record, "read-progress", {"after_sequence": value["after_sequence"], "limit": value["limit"]})

    def cancel_run(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        value = _exact_arguments(arguments, {"run_id"})
        record = self._read_run_record(value["run_id"])
        if not record["launcher_bound"] and record["phase"] not in {"scheduler-intent", "uncertain"}:
            if record["phase"] != "cancelled":
                record = {**record, "phase": "cancelled", "revision": record["revision"] + 1, "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")}
                self._write_run_record(record)
            return self._allocated_observation(record)
        if not record["launcher_bound"]:
            raise _failure("submission-uncertain", "Cancellation cannot safely identify an uncertain scheduler job.", phase="cancellation", allocation_state="unknown", retry_safety="unsafe", next_action="inspect-scheduler", identities={"run_id": record["run_id"]})
        return self._run_controller(record, "cancel-run", {})

    def plan_retry(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        value = _exact_arguments(arguments, {"run_id", "recompute"})
        record = self._read_run_record(value["run_id"])
        if not record["launcher_bound"]:
            raise _failure("retry-conflict", "Only a submitted terminal run can be retried.")
        return self._run_controller(record, "plan-retry", {"recompute": value["recompute"]})

    def start_retry(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        value = _exact_arguments(arguments, {"plan"})
        try:
            from bioimageflow.launcher.retry import RunRetryPlan

            retry = RunRetryPlan.from_dict(value["plan"])
        except (TypeError, ValueError) as exc:
            raise _failure("invalid-retry", "The retained retry plan is invalid.") from exc
        parent = self._read_run_record(retry.parent_run_id)
        observation = self._run_controller(parent, "start-retry", {"plan": retry.to_dict()})
        run_id = observation.get("run_id")
        if run_id != retry.retry_run_id:
            raise _failure("operation-record-tampered", "The retry changed its run binding.")
        run_dir = self._run_directory(run_id)
        if not run_dir.exists():
            run_dir.mkdir(mode=0o700)
            now = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
            cloned = {
                **parent,
                "run_id": run_id,
                "attempt_id": str(uuid.uuid4()),
                "request_digest": canonical_digest({"retry_plan": retry.to_dict()}),
                "phase": "submitted",
                "launcher_bound": True,
                "revision": 0,
                "updated_at": now,
            }
            self._write_run_record(cloned)
        return observation

    def prepare_result(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        value = _exact_arguments(arguments, {"run_id"})
        record = self._read_run_record(value["run_id"])
        if not record["launcher_bound"]:
            raise _failure("result-integrity-failed", "The allocated run has no result.")
        return self._run_controller(record, "prepare-result", {})

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
            "cancel-run",
            "inspect-run",
            "plan-retry",
            "prepare-result",
            "publish_deployment",
            "read-progress",
            "refresh-run",
            "start-retry",
            "submit-plan",
            "validate-deployment",
        ],
        "limits": {
            "max_request_bytes": MAX_REQUEST_BYTES,
            "max_response_bytes": MAX_REQUEST_BYTES,
            "max_upload_bytes": _MAX_UPLOAD_BYTES,
            "max_archive_entries": _MAX_ARCHIVE_ENTRIES,
            "max_archive_expanded_bytes": _MAX_ARCHIVE_EXPANDED_BYTES,
            "max_child_response_bytes": _MAX_CHILD_RESPONSE_BYTES,
            "max_progress_page": 500,
            "max_secret_value_bytes": 64 * 1024,
            "max_secret_total_bytes": 256 * 1024,
        },
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
        "inspect-run": state.inspect_run,
        "refresh-run": state.refresh_run,
        "read-progress": state.read_progress,
        "cancel-run": state.cancel_run,
        "plan-retry": state.plan_retry,
        "start-retry": state.start_retry,
        "prepare-result": state.prepare_result,
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
        elif request.operation == "submit-plan":
            result = state.submit_plan_request(request)
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
