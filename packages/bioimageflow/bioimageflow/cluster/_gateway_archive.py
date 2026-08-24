"""Confined cleanup and invocation-archive gateway support."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import unicodedata
import uuid
import zipfile
from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from typing import Any, cast

from bioimageflow.storage import canonical_json_bytes

from ._common import DIGEST_RE, canonical_digest, thaw_json
from ._gateway_wire import validate_gateway_run_id
from ._gateway_support import (
    _MAX_ARCHIVE_ENTRIES,
    _MAX_ARCHIVE_EXPANDED_BYTES,
    _failure,
    _file_digest,
    _stat_private_directory,
    _validate_published_file,
)

_CLEANUP_PLAN_SCHEMA = "bioimageflow.cluster_cleanup_plan.v1"
_CLEANUP_REPORT_SCHEMA = "bioimageflow.cluster_cleanup_report.v1"
_CLEANUP_NAMESPACES = frozenset(
    {"deployments", "objects", "runs", "temporary", "transfers"}
)
_MAX_INVENTORY_ENTRIES = 100_000
_MAX_LOGICAL_PATH_LENGTH = 4096
_MAX_LOGICAL_PATH_DEPTH = 64
_SCHEDULER_JOB_FIELDS = {
    "schema",
    "scheduler",
    "walltime_seconds",
    "queue",
    "project",
    "cpu",
    "memory_bytes",
    "gpu",
    "attributes",
    "hard_cancel_after_seconds",
}


def _supported_scheduler_job(
    value: Any, *, expected_scheduler: Any
) -> dict[str, Any]:
    if (
        not isinstance(value, Mapping)
        or set(value) != _SCHEDULER_JOB_FIELDS
        or value.get("schema") != "bioimageflow.scheduler_job.v1"
        or value.get("scheduler") != expected_scheduler
    ):
        raise _failure(
            "unsupported-scheduler-adapter",
            "The scheduler request does not match the deployed adapter.",
            phase="validation",
            retry_safety="safe",
            next_action="deploy-matching-scheduler",
        )
    normalized = thaw_json(value)
    assert type(normalized) is dict
    if (
        normalized["gpu"] != 0
        or normalized["memory_bytes"] is not None
        or normalized["attributes"] != {}
    ):
        raise _failure(
            "unsupported-scheduler-adapter",
            "The current PSI/J bridge cannot represent GPU, memory, or custom scheduler resources.",
            phase="validation",
            retry_safety="safe",
            next_action="remove-unsupported-orchestrator-resources",
        )
    return normalized


def _cleanup_tree_snapshot(
    root: Path, relative_path: str, *, physical_path: Path | None = None
) -> tuple[str, int]:
    """Return an identity for one confined tree without following links."""
    logical = PurePosixPath(relative_path)
    if (
        logical.is_absolute()
        or str(logical) != relative_path
        or "\\" in relative_path
        or len(relative_path) > _MAX_LOGICAL_PATH_LENGTH
        or len(logical.parts) < 2
        or len(logical.parts) > _MAX_LOGICAL_PATH_DEPTH
        or logical.parts[0] not in _CLEANUP_NAMESPACES
        or any(part in {"", ".", ".."} for part in logical.parts)
    ):
        raise _failure("cleanup-conflict", "A cleanup candidate path is not confined.")
    namespace = root / logical.parts[0]
    namespace_metadata = _stat_private_directory(namespace)
    target = root.joinpath(*logical.parts) if physical_path is None else physical_path
    entries: list[dict[str, Any]] = []
    total_size = 0

    def visit(path: Path, child_path: str, depth: int) -> None:
        nonlocal total_size
        if (
            depth > _MAX_LOGICAL_PATH_DEPTH
            or len(child_path) > _MAX_LOGICAL_PATH_LENGTH
            or len(entries) >= _MAX_INVENTORY_ENTRIES
        ):
            raise _failure(
                "resource-limit-exceeded", "The cleanup inventory exceeds its limit."
            )
        try:
            metadata = path.stat(follow_symlinks=False)
        except FileNotFoundError as exc:
            raise _failure("cleanup-conflict", "A cleanup candidate disappeared.") from exc
        mode = metadata.st_mode
        if (
            stat.S_ISLNK(mode)
            or metadata.st_dev != namespace_metadata.st_dev
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(mode) & 0o022
        ):
            raise _failure("cleanup-conflict", "A cleanup candidate is unsafe.")
        item = {
            "path": child_path,
            "device": metadata.st_dev,
            "inode": metadata.st_ino,
            "type": "directory" if stat.S_ISDIR(mode) else "file",
            "owner": metadata.st_uid,
            "mode": stat.S_IMODE(mode),
            "link_count": metadata.st_nlink,
            "size": metadata.st_size,
            "digest": None,
        }
        if stat.S_ISREG(mode):
            if metadata.st_nlink != 1:
                raise _failure("cleanup-conflict", "A cleanup file has extra hard links.")
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            try:
                opened = os.fstat(descriptor)
                size, digest = _file_digest(descriptor)
                after = os.fstat(descriptor)
            finally:
                os.close(descriptor)
            if (
                (opened.st_dev, opened.st_ino, opened.st_mode, opened.st_uid,
                 opened.st_nlink, opened.st_size)
                != (metadata.st_dev, metadata.st_ino, metadata.st_mode,
                    metadata.st_uid, metadata.st_nlink, metadata.st_size)
                or (after.st_dev, after.st_ino, after.st_mode, after.st_uid,
                    after.st_nlink, after.st_size)
                != (opened.st_dev, opened.st_ino, opened.st_mode, opened.st_uid,
                    opened.st_nlink, opened.st_size)
            ):
                raise _failure("cleanup-conflict", "A cleanup file changed during inventory.")
            item["size"] = size
            item["digest"] = digest
            total_size += size
        elif stat.S_ISDIR(mode):
            try:
                children = sorted(path.iterdir(), key=lambda child: child.name)
            except OSError as exc:
                raise _failure("cleanup-conflict", "A cleanup directory is unreadable.") from exc
            for child in children:
                visit(child, f"{child_path}/{child.name}", depth + 1)
        else:
            raise _failure("cleanup-conflict", "Cleanup refuses special files.")
        entries.append(item)

    visit(target, relative_path, len(logical.parts))
    return canonical_digest(
        {
            "schema": "bioimageflow.cluster.cleanup_candidate_identity.v1",
            "namespace": logical.parts[0],
            "path": relative_path,
            "entries": sorted(entries, key=lambda item: item["path"]),
        }
    ), total_size


def _unlink_tree_at(parent_descriptor: int, name: str) -> None:
    """Remove one already tombstoned tree relative to a validated directory."""
    metadata = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
    if stat.S_ISREG(metadata.st_mode):
        os.unlink(name, dir_fd=parent_descriptor)
        return
    if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        raise _failure("cleanup-conflict", "A cleanup tombstone is unsafe.")
    descriptor = os.open(
        name,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
        dir_fd=parent_descriptor,
    )
    try:
        for child in sorted(os.listdir(descriptor)):
            _unlink_tree_at(descriptor, child)
    finally:
        os.close(descriptor)
    os.rmdir(name, dir_fd=parent_descriptor)


def _open_cleanup_parent(
    root: Path, logical: PurePosixPath
) -> tuple[int, int, list[int]]:
    """Open the candidate parent from the root without following components."""
    descriptors: list[int] = []
    root_descriptor = os.open(
        root,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    descriptors.append(root_descriptor)
    namespace_descriptor = os.open(
        logical.parts[0],
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
        dir_fd=root_descriptor,
    )
    descriptors.append(namespace_descriptor)
    namespace_metadata = os.fstat(namespace_descriptor)
    if (
        not stat.S_ISDIR(namespace_metadata.st_mode)
        or namespace_metadata.st_uid != os.geteuid()
        or stat.S_IMODE(namespace_metadata.st_mode) & 0o022
    ):
        for descriptor in reversed(descriptors):
            os.close(descriptor)
        raise _failure("cleanup-conflict", "The cleanup namespace is unsafe.")
    parent_descriptor = namespace_descriptor
    try:
        for component in logical.parts[1:-1]:
            descriptor = os.open(
                component,
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=parent_descriptor,
            )
            descriptors.append(descriptor)
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISDIR(metadata.st_mode)
                or metadata.st_dev != namespace_metadata.st_dev
                or metadata.st_uid != os.geteuid()
                or stat.S_IMODE(metadata.st_mode) & 0o022
            ):
                raise _failure("cleanup-conflict", "A cleanup path component is unsafe.")
            parent_descriptor = descriptor
    except BaseException:
        for descriptor in reversed(descriptors):
            os.close(descriptor)
        raise
    return namespace_descriptor, parent_descriptor, descriptors


def _canonical_run_id(value: Any) -> str:
    try:
        return validate_gateway_run_id(value)
    except (TypeError, ValueError) as exc:
        raise _failure("protocol-incompatible", "run_id is invalid.") from exc


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
        parsed = json.loads(canonical_json_bytes(value))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise _failure(
            "protocol-incompatible", "The prepared invocation manifest is invalid."
        ) from exc
    fields = {
        "schema",
        "invocation_digest",
        "workflow_digest",
        "entries",
        "inputs",
        "targets",
        "node_input_overrides",
        "task_policy",
    }
    if (
        type(parsed) is not dict
        or set(parsed) != fields
        or parsed["schema"] != "bioimageflow.prepared_cluster_invocation.v1"
        or type(parsed["invocation_digest"]) is not str
        or DIGEST_RE.fullmatch(parsed["invocation_digest"]) is None
        or type(parsed["workflow_digest"]) is not str
        or DIGEST_RE.fullmatch(parsed["workflow_digest"]) is None
        or type(parsed["entries"]) is not list
        or type(parsed["inputs"]) is not list
        or (
            parsed["targets"] is not None
            and (
                type(parsed["targets"]) is not list
                or any(type(item) is not str or not item for item in parsed["targets"])
            )
        )
        or type(parsed["node_input_overrides"]) is not list
        or type(parsed["task_policy"]) is not dict
    ):
        raise _failure(
            "protocol-incompatible", "The prepared invocation manifest is invalid."
        )
    entry_fields = {"path", "kind", "size", "digest"}
    for entry in parsed["entries"]:
        if (
            type(entry) is not dict
            or set(entry) != entry_fields
            or type(entry["path"]) is not str
            or not entry["path"]
            or PurePosixPath(entry["path"]).is_absolute()
            or str(PurePosixPath(entry["path"])) != entry["path"]
            or any(part in {"", ".", ".."} for part in PurePosixPath(entry["path"]).parts)
            or entry["kind"] not in {"file", "directory"}
            or type(entry["size"]) is not int
            or entry["size"] < 0
            or type(entry["digest"]) is not str
            or DIGEST_RE.fullmatch(entry["digest"]) is None
        ):
            raise _failure(
                "protocol-incompatible", "The prepared invocation manifest is invalid."
            )
    if parsed["invocation_digest"] != expected_digest:
        raise _failure(
            "operation-conflict", "The execution plan names another invocation."
        )
    return parsed


def _validate_invocation_archive(
    path: Path, manifest: Mapping[str, Any], expected_object_id: str
) -> None:
    _validate_published_file(path, expected_object_id)
    entries = manifest.get("entries")
    if type(entries) is not list:
        raise _failure("deployment-tampered", "The invocation inventory is invalid.")
    declared = {cast(str, entry.get("path")): entry for entry in entries if type(entry) is dict}
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
    entries = manifest.get("entries")
    if type(entries) is not list:
        raise _failure("deployment-tampered", "The invocation inventory is invalid.")
    declared = {cast(str, entry.get("path")): entry for entry in entries if type(entry) is dict}
    declared_files = {
        name: entry for name, entry in declared.items() if entry.get("kind") == "file"
    }
    declared_directories = {
        name for name, entry in declared.items() if entry.get("kind") == "directory"
    }
    try:
        archive = zipfile.ZipFile(path)
    except (OSError, zipfile.BadZipFile) as exc:
        raise _failure("deployment-tampered", "The invocation archive is invalid.") from exc
    try:
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
            if set(files) != set(declared_files):
                raise _failure("deployment-tampered", "The invocation archive inventory changed.")

            destination.mkdir(mode=0o700)
            for name in sorted(
                declared_directories,
                key=lambda item: len(PurePosixPath(item).parts),
            ):
                relative = PurePosixPath(name)
                parent = relative.parent.as_posix()
                if parent != "." and parent not in declared_directories:
                    raise _failure("deployment-tampered", "The invocation inventory is invalid.")
                destination.joinpath(*relative.parts).mkdir(mode=0o700)

            for name, member in files.items():
                relative = PurePosixPath(name)
                parent = relative.parent.as_posix()
                if parent != "." and parent not in declared_directories:
                    raise _failure("deployment-tampered", "The invocation inventory is invalid.")
                entry = declared_files[name]
                output_path = destination.joinpath(*relative.parts)
                descriptor = os.open(
                    output_path,
                    os.O_WRONLY
                    | os.O_CREAT
                    | os.O_EXCL
                    | getattr(os, "O_NOFOLLOW", 0),
                    0o600,
                )
                digest = hashlib.sha256()
                size = 0
                try:
                    with archive.open(member) as stream:
                        while chunk := stream.read(1024 * 1024):
                            size += len(chunk)
                            digest.update(chunk)
                            offset = 0
                            while offset < len(chunk):
                                offset += os.write(descriptor, chunk[offset:])
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
                if (
                    size != entry.get("size")
                    or size != member.file_size
                    or f"sha256:{digest.hexdigest()}" != entry.get("digest")
                ):
                    raise _failure("deployment-tampered", "An invocation entry changed.")
    except BaseException:
        shutil.rmtree(destination, ignore_errors=True)
        raise
