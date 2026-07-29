"""Durable cluster-side upload allocation and object installation."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import stat
import unicodedata
import uuid
from pathlib import Path, PurePosixPath
from typing import Any, Callable

from bioimageflow.storage import canonical_json_bytes, validate_relative_posix_path
from bioimageflow.storage.dataframe_transport import file_sha256

from .cluster_bundle import MANIFEST_SCHEMA, MAX_UPLOAD_DEPTH
from .cluster_protocol import ClusterProtocolFailure
from .repository import _CrossProcessLock, _atomic_write_json, _read_json, _sync_directory


def normalized_root(value: Any) -> Path:
    if type(value) is not str:
        raise ClusterProtocolFailure(
            "invalid-staging-root",
            "staging_root must be a normalized absolute POSIX path.",
        )
    pure = PurePosixPath(value)
    if (
        any(character in value for character in ("\x00", "\n", "\r"))
        or not pure.is_absolute()
        or value.startswith("//")
        or str(pure) != value
        or any(part in {"", ".", ".."} for part in pure.parts[1:])
    ):
        raise ClusterProtocolFailure(
            "invalid-staging-root",
            "staging_root must be a normalized absolute POSIX path.",
        )
    return Path(value)


def _ensure_root(root: Path) -> None:
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        root_mode = root.lstat().st_mode
    except OSError as exc:
        raise ClusterProtocolFailure(
            "unsafe-staging-root",
            "Transport staging root is unavailable.",
        ) from exc
    if stat.S_ISLNK(root_mode) or not stat.S_ISDIR(root_mode):
        raise ClusterProtocolFailure(
            "unsafe-staging-root",
            "Transport staging root must be a non-symlink directory.",
        )
    for child in (".partial", "ready", "objects", "receipts", "locks"):
        path = root / child
        path.mkdir(mode=0o700, exist_ok=True)
        mode = path.lstat().st_mode
        if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
            raise ClusterProtocolFailure(
                "unsafe-staging-root",
                "Transport staging contains an unsafe directory.",
            )


def _ensure_confined_directory(path: Path, *, root: Path) -> None:
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise ClusterProtocolFailure(
            "unsafe-staging-root",
            "Transport staging path escapes its root.",
        ) from exc
    current = root
    for part in path.relative_to(root).parts:
        current /= part
        current.mkdir(mode=0o700, exist_ok=True)
        mode = current.lstat().st_mode
        if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
            raise ClusterProtocolFailure(
                "unsafe-staging-root",
                "Transport staging contains an unsafe directory.",
            )


def validate_manifest(value: Any) -> dict[str, Any]:
    if type(value) is not dict or set(value) != {
        "digest",
        "entries",
        "root_name",
        "schema",
    }:
        raise ClusterProtocolFailure(
            "invalid-upload-manifest",
            "Upload manifest contains missing or unknown fields.",
        )
    if value["schema"] != MANIFEST_SCHEMA or value["root_name"] != "submission":
        raise ClusterProtocolFailure(
            "invalid-upload-manifest",
            "Upload manifest schema or root name is invalid.",
        )
    entries = value["entries"]
    if type(entries) is not list or len(entries) > 100_000:
        raise ClusterProtocolFailure(
            "invalid-upload-manifest",
            "Upload manifest entries are invalid.",
        )
    seen: set[str] = set()
    folded_paths: set[str] = set()
    total = 0
    paths: list[str] = []
    for item in entries:
        if type(item) is not dict or set(item) != {
            "digest",
            "kind",
            "path",
            "size",
        }:
            raise ClusterProtocolFailure(
                "invalid-upload-manifest",
                "Upload manifest entry shape is invalid.",
            )
        try:
            path = validate_relative_posix_path(item["path"])
        except (TypeError, ValueError) as exc:
            raise ClusterProtocolFailure(
                "invalid-upload-manifest",
                "Upload manifest path is unsafe.",
            ) from exc
        folded = path.casefold()
        if (
            path in seen
            or folded in folded_paths
            or unicodedata.normalize("NFC", path) != path
            or len(PurePosixPath(path).parts) > MAX_UPLOAD_DEPTH
            or item["kind"] not in {"directory", "file"}
            or type(item["size"]) is not int
            or item["size"] < 0
            or type(item["digest"]) is not str
            or re.fullmatch(r"sha256:[0-9a-f]{64}", item["digest"]) is None
        ):
            raise ClusterProtocolFailure(
                "invalid-upload-manifest",
                "Upload manifest entry value is invalid.",
            )
        if item["kind"] == "directory" and (
            item["size"] != 0
            or item["digest"]
            != f"sha256:{hashlib.sha256(b'').hexdigest()}"
        ):
            raise ClusterProtocolFailure(
                "invalid-upload-manifest",
                "Upload directory manifest entry is invalid.",
            )
        seen.add(path)
        folded_paths.add(folded)
        paths.append(path)
        total += item["size"]
        if total > 1 << 40:
            raise ClusterProtocolFailure(
                "upload-too-large",
                "Upload manifest exceeds the aggregate byte limit.",
            )
    if paths != sorted(paths):
        raise ClusterProtocolFailure(
            "invalid-upload-manifest",
            "Upload manifest entries must be in canonical path order.",
        )
    body = {
        "entries": entries,
        "root_name": value["root_name"],
        "schema": value["schema"],
    }
    digest = f"sha256:{hashlib.sha256(canonical_json_bytes(body)).hexdigest()}"
    if value["digest"] != digest:
        raise ClusterProtocolFailure(
            "upload-manifest-digest-mismatch",
            "Upload manifest digest does not match.",
        )
    return value


def _receipt(
    root: Path,
    operation: str,
    request_id: str,
    digest: str,
    create: Callable[[], dict[str, Any]],
) -> dict[str, Any]:
    receipt_dir = root / "receipts" / operation
    _ensure_confined_directory(receipt_dir, root=root)
    receipt_path = receipt_dir / f"{request_id}.json"
    lock = root / "locks" / f"{operation}-{request_id}.lock"
    with _CrossProcessLock(lock):
        if receipt_path.exists():
            persisted = _read_json(receipt_path)
            expected_fields = {
                "operation",
                "request_digest",
                "request_id",
                "result",
                "schema",
            }
            if (
                set(persisted) != expected_fields
                or persisted["operation"] != operation
                or persisted["request_id"] != request_id
                or persisted["schema"] != "bioimageflow.cluster.receipt.v1"
            ):
                raise ClusterProtocolFailure(
                    "corrupt-receipt",
                    "Transport receipt is malformed.",
                )
            if persisted["request_digest"] != digest:
                raise ClusterProtocolFailure(
                    "duplicate-request-conflict",
                    "request_id was already used with different arguments.",
                )
            result = persisted["result"]
            if type(result) is not dict:
                raise ClusterProtocolFailure(
                    "corrupt-receipt",
                    "Transport receipt is malformed.",
                )
            return result
        result = create()
        _atomic_write_json(
            receipt_path,
            {
                "operation": operation,
                "request_digest": digest,
                "request_id": request_id,
                "result": result,
                "schema": "bioimageflow.cluster.receipt.v1",
            },
        )
        return result


def allocate_upload(
    root: Path,
    request_id: str,
    digest: str,
    manifest: Any,
) -> dict[str, Any]:
    _ensure_root(root)
    validate_manifest(manifest)

    def create() -> dict[str, Any]:
        upload_id = str(uuid.uuid4())
        partial = root / ".partial" / upload_id
        partial.mkdir(mode=0o700)
        _sync_directory(partial.parent)
        return {
            "remote_root": partial.as_posix(),
            "upload_id": upload_id,
        }

    return _receipt(root, "allocate-upload", request_id, digest, create)


def _verify_tree(
    root: Path,
    manifest: dict[str, Any],
    *,
    require_read_only: bool = False,
) -> None:
    try:
        root_mode = root.lstat().st_mode
    except OSError as exc:
        raise ClusterProtocolFailure(
            "upload-integrity",
            "Uploaded tree root is unavailable.",
        ) from exc
    if stat.S_ISLNK(root_mode) or not stat.S_ISDIR(root_mode):
        raise ClusterProtocolFailure(
            "upload-integrity",
            "Uploaded tree root must be a non-symlink directory.",
        )
    if require_read_only and root_mode & 0o222:
        raise ClusterProtocolFailure(
            "upload-integrity",
            "Installed upload object is not read-only.",
        )
    expected = {entry["path"]: entry for entry in manifest["entries"]}
    actual: set[str] = set()
    for path in root.rglob("*"):
        try:
            mode = path.lstat().st_mode
        except OSError as exc:
            raise ClusterProtocolFailure(
                "upload-integrity",
                "Uploaded tree changed during verification.",
            ) from exc
        if stat.S_ISLNK(mode):
            raise ClusterProtocolFailure(
                "upload-integrity",
                "Uploaded tree contains a symlink.",
            )
        relative = path.relative_to(root).as_posix()
        entry = expected.get(relative)
        actual.add(relative)
        if stat.S_ISDIR(mode):
            if entry is None or entry["kind"] != "directory":
                raise ClusterProtocolFailure(
                    "upload-integrity",
                    "Uploaded directory is not declared by the manifest.",
                )
            if require_read_only and mode & 0o222:
                raise ClusterProtocolFailure(
                    "upload-integrity",
                    "Installed upload object is not read-only.",
                )
            continue
        if not stat.S_ISREG(mode):
            raise ClusterProtocolFailure(
                "upload-integrity",
                "Uploaded tree contains a special file.",
            )
        if require_read_only and mode & 0o222:
            raise ClusterProtocolFailure(
                "upload-integrity",
                "Installed upload object is not read-only.",
            )
        if (
            entry is None
            or entry["kind"] != "file"
            or path.stat().st_size != entry["size"]
            or file_sha256(path) != entry["digest"]
        ):
            raise ClusterProtocolFailure(
                "upload-integrity",
                "Uploaded file does not match the committed manifest.",
            )
    if actual != set(expected):
        raise ClusterProtocolFailure(
            "upload-integrity",
            "Uploaded tree contains missing or extra files.",
        )


def _make_read_only(root: Path) -> None:
    for path in sorted(root.rglob("*"), reverse=True):
        mode = path.stat(follow_symlinks=False).st_mode
        os.chmod(path, 0o555 if stat.S_ISDIR(mode) else 0o444)
    os.chmod(root, 0o555)


def commit_upload(
    root: Path,
    request_id: str,
    digest: str,
    upload_id: Any,
    manifest: Any,
) -> dict[str, Any]:
    _ensure_root(root)
    validated = validate_manifest(manifest)
    try:
        canonical_upload_id = str(uuid.UUID(upload_id, version=4))
    except (AttributeError, TypeError, ValueError) as exc:
        raise ClusterProtocolFailure(
            "invalid-upload-id",
            "upload_id must be a canonical UUID4 string.",
        ) from exc
    if canonical_upload_id != upload_id:
        raise ClusterProtocolFailure(
            "invalid-upload-id",
            "upload_id must be a canonical UUID4 string.",
        )

    def create() -> dict[str, Any]:
        with _CrossProcessLock(root / "locks" / f"upload-{upload_id}.lock"):
            partial = root / ".partial" / upload_id
            ready = root / "ready" / upload_id
            if partial.exists():
                _verify_tree(partial, validated)
                os.replace(partial, ready)
                _sync_directory(ready.parent)
            elif not ready.exists():
                raise ClusterProtocolFailure(
                    "upload-not-found",
                    "Allocated upload is not available to commit.",
                )
            _verify_tree(ready, validated)
        digest_token = validated["digest"].removeprefix("sha256:")
        with _CrossProcessLock(root / "locks" / f"object-{digest_token}.lock"):
            object_parent = root / "objects" / "sha256" / digest_token
            _ensure_confined_directory(object_parent, root=root)
            object_root = object_parent / "submission"
            if object_root.exists():
                _verify_tree(
                    object_root,
                    validated,
                    require_read_only=True,
                )
            else:
                temporary = object_parent / f".{uuid.uuid4().hex}.tmp"
                shutil.copytree(ready, temporary)
                _verify_tree(temporary, validated)
                _make_read_only(temporary)
                _verify_tree(
                    temporary,
                    validated,
                    require_read_only=True,
                )
                try:
                    os.rename(temporary, object_root)
                except FileExistsError:
                    shutil.rmtree(temporary)
                    _verify_tree(
                        object_root,
                        validated,
                        require_read_only=True,
                    )
                _sync_directory(object_root.parent)
        return {
            "bundle_digest": validated["digest"],
            "object_path": object_root.as_posix(),
            "upload_id": upload_id,
        }

    return _receipt(root, "commit-upload", request_id, digest, create)
