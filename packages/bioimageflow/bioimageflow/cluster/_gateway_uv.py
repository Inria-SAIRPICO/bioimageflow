"""Frozen managed-uv realization for the cluster gateway."""

from __future__ import annotations

import hashlib
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import zipfile
from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from typing import Any

from ._common import canonical_digest
from ._gateway_support import _child_environment, _failure, _run_json_child

_MANAGED_ATTESTATION_SCRIPT = r'''from __future__ import annotations
import importlib.metadata as metadata
import json
import platform
import sys

packages = {}
for distribution in metadata.distributions():
    name = distribution.metadata.get("Name")
    if name:
        canonical = "-".join(filter(None, __import__("re").split(r"[-_.]+", name.lower())))
        if canonical in packages and packages[canonical] != distribution.version:
            raise RuntimeError("duplicate installed distribution identity")
        packages[canonical] = distribution.version
try:
    import psij
    executors = sorted(psij.JobExecutor.get_executor_names())
except Exception:
    executors = []
print(json.dumps({
    "schema": "bioimageflow.cluster.managed_uv_attestation.v1",
    "implementation": sys.implementation.name,
    "cache_tag": sys.implementation.cache_tag,
    "version": platform.python_version(),
    "version_info": list(sys.version_info[:3]),
    "platform_system": platform.system(),
    "platform_machine": platform.machine(),
    "packages": dict(sorted(packages.items())),
    "psij_executor_names": executors,
}, sort_keys=True, separators=(",", ":"), allow_nan=False))
'''


def _canonical_name(value: str) -> str:
    import re

    return re.sub(r"[-_.]+", "-", value).lower()


def _artifact_path(content: Path, value: Any) -> Path:
    if type(value) is not str:
        raise _failure("environment-artifact-missing", "A wheel path is missing.")
    relative = PurePosixPath(value)
    if (
        relative.is_absolute()
        or not relative.parts
        or any(part in {"", ".", ".."} for part in relative.parts)
    ):
        raise _failure("environment-artifact-missing", "A wheel path is invalid.")
    path = content.joinpath(*relative.parts)
    try:
        metadata = path.stat(follow_symlinks=False)
    except OSError as exc:
        raise _failure(
            "environment-artifact-missing", "A captured wheel is unavailable."
        ) from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise _failure(
            "environment-artifact-missing", "A captured wheel is not a regular file."
        )
    return path


def _verify_artifact(content: Path, artifact: Mapping[str, Any]) -> Path:
    path = _artifact_path(content, artifact.get("path"))
    expected_size = artifact.get("size")
    expected_digest = artifact.get("digest")
    if (
        type(expected_size) is not int
        or expected_size <= 0
        or type(expected_digest) is not str
        or not expected_digest.startswith("sha256:")
    ):
        raise _failure(
            "environment-artifact-missing", "A captured wheel identity is invalid."
        )
    digest = hashlib.sha256()
    size = 0
    try:
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                size += len(chunk)
                if size > expected_size:
                    break
                digest.update(chunk)
    except OSError as exc:
        raise _failure(
            "environment-artifact-missing", "A captured wheel could not be verified."
        ) from exc
    if size != expected_size or f"sha256:{digest.hexdigest()}" != expected_digest:
        raise _failure(
            "environment-artifact-missing", "A captured wheel changed after preparation."
        )
    return path


def _target_descriptor() -> dict[str, Any]:
    return {
        "implementation": sys.implementation.name,
        "cache_tag": sys.implementation.cache_tag,
        "version": platform.python_version(),
        "version_info": list(sys.version_info[:3]),
        "platform_system": platform.system(),
        "platform_machine": platform.machine(),
    }


def _select_uv_installer(
    candidate: Path, content: Path, installer: Mapping[str, Any]
) -> Path:
    try:
        from packaging.tags import sys_tags
        from packaging.utils import parse_wheel_filename
    except ModuleNotFoundError as exc:
        raise _failure(
            "environment-installer-unavailable",
            "Managed uv installation requires the packaging runtime.",
        ) from exc
    artifacts = installer.get("artifacts")
    if not isinstance(artifacts, list):
        raise _failure(
            "environment-installer-unavailable", "The pinned uv installer plan is invalid."
        )
    ranks = {tag: index for index, tag in enumerate(sys_tags())}
    compatible: list[tuple[int, str, Path]] = []
    for artifact in artifacts:
        if not isinstance(artifact, Mapping):
            raise _failure(
                "environment-installer-unavailable",
                "A pinned uv installer artifact is invalid.",
            )
        wheel = _verify_artifact(content, artifact)
        try:
            name, version, _build, tags = parse_wheel_filename(wheel.name)
        except ValueError as exc:
            raise _failure(
                "environment-installer-unavailable", "A pinned uv wheel is malformed."
            ) from exc
        if (
            _canonical_name(str(name)) != "uv"
            or str(version) != installer.get("version")
        ):
            raise _failure(
                "environment-installer-unavailable",
                "A pinned uv wheel has the wrong installer identity.",
            )
        matching = [ranks[tag] for tag in tags if tag in ranks]
        if matching:
            compatible.append((min(matching), wheel.name, wheel))
    if not compatible:
        raise _failure(
            "environment-platform-incompatible",
            "No pinned uv installer artifact matches the gateway target.",
        )
    compatible.sort(key=lambda item: (item[0], item[1]))
    wheel = compatible[0][2]
    executable = candidate / ".installer" / "uv"
    executable.parent.mkdir(mode=0o700)
    try:
        with zipfile.ZipFile(wheel) as archive:
            matches = [item for item in archive.infolist() if item.filename == "uv/uv"]
            if len(matches) != 1:
                raise _failure(
                    "environment-installer-unavailable",
                    "The pinned uv wheel has no unique executable.",
                )
            member = matches[0]
            mode = member.external_attr >> 16
            if (
                member.flag_bits & 0x1
                or member.is_dir()
                or stat.S_ISLNK(mode)
                or member.file_size <= 0
                or member.file_size > 256 * 1024 * 1024
                or member.compress_size <= 0
                or member.file_size > max(member.compress_size * 500, 1024 * 1024)
            ):
                raise _failure(
                    "environment-installer-unavailable",
                    "The pinned uv executable archive member is unsafe.",
                )
            content_bytes = archive.read(member)
        descriptor = os.open(
            executable,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o700,
        )
        try:
            offset = 0
            while offset < len(content_bytes):
                offset += os.write(descriptor, content_bytes[offset:])
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except (OSError, zipfile.BadZipFile) as exc:
        raise _failure(
            "environment-installer-unavailable",
            "The pinned uv executable could not be extracted.",
        ) from exc
    return executable


def _select_wheels(
    content: Path, plan: Mapping[str, Any], manifest: Mapping[str, Any]
) -> tuple[list[Path], dict[str, str]]:
    try:
        from packaging.specifiers import SpecifierSet
        from packaging.tags import sys_tags
        from packaging.utils import parse_wheel_filename
    except ModuleNotFoundError as exc:
        raise _failure(
            "environment-installer-unavailable",
            "Managed uv installation requires the packaging runtime.",
        ) from exc
    requires_python = plan.get("requires_python")
    try:
        compatible_python = requires_python is None or (
            type(requires_python) is str
            and platform.python_version() in SpecifierSet(requires_python)
        )
    except Exception as exc:
        raise _failure(
            "environment-lock-invalid", "The frozen Python requirement is invalid."
        ) from exc
    if not compatible_python:
        raise _failure(
            "environment-platform-incompatible",
            "The gateway Python does not satisfy the frozen uv project.",
        )
    target_tags = tuple(sys_tags())
    tag_rank = {tag: index for index, tag in enumerate(target_tags)}
    expected: dict[str, str] = {}
    selected: dict[str, Path] = {}

    local_artifacts = plan.get("local_artifacts")
    if not isinstance(local_artifacts, list):
        raise _failure("environment-lock-invalid", "The local wheel plan is invalid.")
    for local in local_artifacts:
        if (
            not isinstance(local, Mapping)
            or type(local.get("name")) is not str
            or type(local.get("version")) is not str
            or not isinstance(local.get("wheel"), Mapping)
        ):
            raise _failure("environment-lock-invalid", "A local wheel plan is invalid.")
        name = _canonical_name(local["name"])
        wheel = local["wheel"]
        path = _verify_artifact(content, wheel)
        try:
            wheel_name, wheel_version, _build, tags = parse_wheel_filename(path.name)
        except ValueError as exc:
            raise _failure(
                "environment-artifact-missing", "A captured local wheel is malformed."
            ) from exc
        if (
            _canonical_name(str(wheel_name)) != name
            or str(wheel_version) != local["version"]
            or not set(tags).intersection(tag_rank)
            or name in selected
        ):
            raise _failure(
                "environment-platform-incompatible",
                "A captured local wheel does not uniquely match the gateway target.",
            )
        selected[name] = path
        expected[name] = local["version"]

    locked_packages = plan.get("locked_packages")
    if not isinstance(locked_packages, list):
        raise _failure("environment-lock-invalid", "The locked package plan is invalid.")
    seen_locked: set[str] = set()
    for package in locked_packages:
        if (
            not isinstance(package, Mapping)
            or type(package.get("name")) is not str
            or type(package.get("version")) is not str
            or type(package.get("source_kind")) is not str
            or not isinstance(package.get("artifacts"), list)
        ):
            raise _failure("environment-lock-invalid", "A locked package is invalid.")
        name = _canonical_name(package["name"])
        if name in seen_locked:
            raise _failure(
                "environment-platform-incompatible",
                "The frozen uv resolution is ambiguous for this target.",
            )
        seen_locked.add(name)
        if name in selected:
            if expected[name] != package["version"]:
                raise _failure(
                    "bioimageflow-version-conflict" if name == "bioimageflow" else "environment-lock-invalid",
                    "A local wheel conflicts with the frozen uv resolution.",
                )
            continue
        if package["source_kind"] == "virtual":
            continue
        if package["source_kind"] != "registry":
            raise _failure(
                "environment-build-lock-incomplete",
                "The frozen uv plan lacks an immutable wheel for a selected package.",
            )
        candidates: list[tuple[int, str, Path]] = []
        for artifact in package["artifacts"]:
            if not isinstance(artifact, Mapping):
                raise _failure(
                    "environment-lock-invalid", "A registry wheel plan is invalid."
                )
            path = _verify_artifact(content, artifact)
            try:
                wheel_name, wheel_version, _build, tags = parse_wheel_filename(path.name)
            except ValueError as exc:
                raise _failure(
                    "environment-artifact-missing", "A captured registry wheel is malformed."
                ) from exc
            if (
                _canonical_name(str(wheel_name)) != name
                or str(wheel_version) != package["version"]
            ):
                raise _failure(
                    "environment-artifact-missing",
                    "A captured registry wheel has the wrong package identity.",
                )
            ranks = [tag_rank[tag] for tag in tags if tag in tag_rank]
            if ranks:
                candidates.append((min(ranks), path.name, path))
        if not candidates:
            raise _failure(
                "environment-platform-incompatible",
                f"No locked wheel for {name!r} matches the gateway target.",
            )
        candidates.sort(key=lambda item: (item[0], item[1]))
        selected[name] = candidates[0][2]
        expected[name] = package["version"]

    bioimageflow_version = manifest.get("bioimageflow_version")
    if expected.get("bioimageflow") != bioimageflow_version:
        raise _failure(
            "bioimageflow-version-conflict",
            "The frozen uv plan lacks the authoritative BioImageFlow wheel.",
        )
    return [selected[name] for name in sorted(selected)], expected


def _validate_required_runtime(
    plan: Mapping[str, Any], manifest: Mapping[str, Any], expected: Mapping[str, str]
) -> None:
    required = {"bioimageflow", "bioimageflow-core", "parsl", "psij-python"}
    plugin = plan.get("psij_scheduler_plugin")
    if (
        not required.issubset(expected)
        or type(plugin) is not dict
        or plugin
        != {
            "scheduler": manifest.get("scheduler"),
            "distribution": "psij-python",
            "version": expected.get("psij-python"),
        }
    ):
        raise _failure(
            "environment-build-lock-incomplete",
            "The frozen uv plan omits an exact required runtime or scheduler plugin.",
        )


def _run_uv_command(executable: Path, arguments: list[str], *, cache: Path) -> None:
    environment = _child_environment(
        {
            "UV_CACHE_DIR": str(cache),
            "UV_OFFLINE": "1",
            "UV_NO_PROGRESS": "1",
            "UV_PYTHON_DOWNLOADS": "never",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )
    try:
        completed = subprocess.run(
            [str(executable), *arguments],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=300,
            env=environment,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise _failure(
            "deployment-install-failed", "The frozen uv installation could not complete."
        ) from exc
    if completed.returncode != 0:
        raise _failure(
            "deployment-install-failed", "The frozen uv installation failed."
        )


def _verify_uv_version(executable: Path, expected: Any) -> None:
    if type(expected) is not str:
        raise _failure(
            "environment-installer-unavailable", "The pinned uv version is invalid."
        )
    try:
        completed = subprocess.run(
            [str(executable), "--version"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=10,
            text=True,
            env=_child_environment({"UV_PYTHON_DOWNLOADS": "never"}),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise _failure(
            "environment-installer-unavailable", "The pinned uv installer cannot run."
        ) from exc
    match = re.fullmatch(
        r"uv ([0-9]+(?:\.[0-9]+){1,3})(?: .*)?\n?", completed.stdout
    )
    if completed.returncode != 0 or match is None or match.group(1) != expected:
        raise _failure(
            "environment-installer-unavailable",
            "The pinned uv installer version differs from its plan.",
        )


def _environment_inventory(root: Path) -> str:
    entries: list[dict[str, Any]] = []
    try:
        for path in sorted(root.rglob("*")):
            relative = path.relative_to(root).as_posix()
            metadata = path.lstat()
            if stat.S_ISDIR(metadata.st_mode):
                kind = "directory"
                digest = None
                size = 0
            elif stat.S_ISLNK(metadata.st_mode):
                kind = "symlink"
                target = os.readlink(path)
                if os.path.isabs(target):
                    # Interpreter links may bind the observed bootstrap interpreter.
                    target = f"absolute:{target}"
                digest = canonical_digest({"target": target})
                size = len(target.encode())
            elif stat.S_ISREG(metadata.st_mode):
                kind = "file"
                content = path.read_bytes()
                digest = f"sha256:{hashlib.sha256(content).hexdigest()}"
                size = len(content)
            else:
                raise _failure(
                    "deployment-install-failed",
                    "The installed uv environment contains an unsupported file type.",
                )
            entries.append(
                {"path": relative, "kind": kind, "size": size, "digest": digest}
            )
    except OSError as exc:
        raise _failure(
            "deployment-tampered", "The installed uv environment could not be verified."
        ) from exc
    return canonical_digest({"schema": "bioimageflow.environment_inventory.v1", "entries": entries})


def _attest_environment(
    environment: Path,
    expected: Mapping[str, str],
    scheduler: Any,
    *,
    failure_category: str,
) -> tuple[dict[str, Any], str]:
    python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if not python.is_file() or not os.access(python, os.X_OK):
        raise _failure(failure_category, "The managed Python interpreter is unavailable.")
    attestation = _run_json_child(
        [str(python), "-I", "-B", "-c", _MANAGED_ATTESTATION_SCRIPT],
        environment=_child_environment(),
        timeout=30,
        failure_category=failure_category,
    )
    fields = {
        "schema",
        "implementation",
        "cache_tag",
        "version",
        "version_info",
        "platform_system",
        "platform_machine",
        "packages",
        "psij_executor_names",
    }
    if (
        set(attestation) != fields
        or attestation.get("schema")
        != "bioimageflow.cluster.managed_uv_attestation.v1"
        or attestation.get("packages") != dict(sorted(expected.items()))
        or type(attestation.get("psij_executor_names")) is not list
        or scheduler not in attestation["psij_executor_names"]
    ):
        raise _failure(
            failure_category,
            "The managed uv environment differs from its exact runtime closure.",
        )
    return attestation, canonical_digest(attestation)


def realize_managed_uv(
    candidate: Path, content: Path, manifest: Mapping[str, Any]
) -> tuple[dict[str, Any], str, str]:
    """Create and attest an unpublished environment using only captured artifacts."""
    plan = manifest.get("environment_plan")
    if (
        not isinstance(plan, Mapping)
        or plan.get("schema") != "bioimageflow.uv_install_plan.v1"
        or plan.get("frozen") is not True
        or plan.get("network_resolution") is not False
        or plan.get("target_policy") != "captured-wheels-target-selected"
        or not isinstance(plan.get("installer"), Mapping)
    ):
        raise _failure("environment-lock-invalid", "The frozen uv install plan is invalid.")
    installer = plan["installer"]
    uv = _select_uv_installer(candidate, content, installer)
    _verify_uv_version(uv, installer.get("version"))
    wheels, expected = _select_wheels(content, plan, manifest)
    _validate_required_runtime(plan, manifest, expected)
    environment = candidate / "environment"
    cache = candidate / ".uv-cache"
    _run_uv_command(
        uv,
        [
            "venv",
            "--offline",
            "--no-config",
            "--no-project",
            "--no-python-downloads",
            "--relocatable",
            "--python",
            sys.executable,
            str(environment),
        ],
        cache=cache,
    )
    python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    _run_uv_command(
        uv,
        [
            "pip",
            "install",
            "--offline",
            "--no-config",
            "--no-index",
            "--no-deps",
            "--no-cache",
            "--python",
            str(python),
            *[str(wheel) for wheel in wheels],
        ],
        cache=cache,
    )
    shutil.rmtree(cache, ignore_errors=True)
    attestation, attestation_digest = _attest_environment(
        environment,
        expected,
        manifest.get("scheduler"),
        failure_category="deployment-install-failed",
    )
    inventory_digest = _environment_inventory(environment)
    for path in sorted(environment.rglob("*"), reverse=True):
        if path.is_symlink():
            continue
        mode = 0o500 if path.is_dir() or os.access(path, os.X_OK) else 0o400
        os.chmod(path, mode, follow_symlinks=False)
    os.chmod(environment, 0o500, follow_symlinks=False)
    return attestation, attestation_digest, inventory_digest


def attest_managed_uv(
    deployment: Path,
    manifest: Mapping[str, Any],
    publication: Mapping[str, Any],
    *,
    failure_category: str = "environment-platform-incompatible",
) -> tuple[dict[str, Any], str, str]:
    """Revalidate immutable bytes and runtime identities for a published uv environment."""
    plan = manifest.get("environment_plan")
    if not isinstance(plan, Mapping):
        raise _failure("environment-lock-invalid", "The frozen uv install plan is absent.")
    _wheels, expected = _select_wheels(deployment / "content", plan, manifest)
    _validate_required_runtime(plan, manifest, expected)
    environment = deployment / "environment"
    inventory_digest = _environment_inventory(environment)
    if inventory_digest != publication.get("environment_inventory_digest"):
        raise _failure(
            "environment-platform-incompatible",
            "The published managed uv environment changed after installation.",
        )
    attestation, digest = _attest_environment(
        environment,
        expected,
        manifest.get("scheduler"),
        failure_category=failure_category,
    )
    return attestation, digest, str(environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python"))


__all__ = ["attest_managed_uv", "realize_managed_uv"]
