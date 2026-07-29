"""Canonical laptop-side bundles for one transported submission."""

from __future__ import annotations

import hashlib
import os
import stat
import tempfile
import unicodedata
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

import pandas as pd

from bioimageflow.parsl import ExecutorBinding, ParslTaskPolicy
from bioimageflow.storage import canonical_json_bytes
from bioimageflow.storage.dataframe_transport import (
    file_sha256,
    write_dataframe_transport,
)
from bioimageflow.validation import is_path_type
from bioimageflow.workflow import Workflow

from .inputs import encode_cluster_typed_constant
from .payload import serialize_workflow_payload
from .submission import _normalize_bindings, _normalize_routes
from .types import LocalUpload, PSIJLaunchConfig, ParslConfigRef


BUNDLE_SCHEMA = "bioimageflow.cluster.submission_bundle.v1"
MANIFEST_SCHEMA = "bioimageflow.cluster.upload_manifest.v1"
MAX_UPLOAD_FILES = 100_000
MAX_UPLOAD_DEPTH = 64
MAX_UPLOAD_BYTES = 1 << 40
_EMPTY_DIGEST = f"sha256:{hashlib.sha256(b'').hexdigest()}"


@dataclass(frozen=True, slots=True)
class PreparedClusterBundle:
    """One temporary bundle ready for server allocation and SFTP."""

    root: Path
    manifest: dict[str, Any]

    @property
    def digest(self) -> str:
        return self.manifest["digest"]


def _safe_basename(path: Path) -> str:
    name = unicodedata.normalize("NFC", path.name)
    if (
        not name
        or name in {".", ".."}
        or "/" in name
        or "\\" in name
        or any(character in name for character in ("\x00", "\n", "\r"))
    ):
        raise ValueError("LocalUpload requires one safe NFC basename.")
    return name


def _copy_entry(
    path: Path,
    destination: Path,
    relative: str,
) -> dict[str, Any]:
    before = path.stat(follow_symlinks=False)
    if not stat.S_ISREG(before.st_mode):
        raise ValueError(f"LocalUpload contains a non-regular file: {path}.")
    digest = hashlib.sha256()
    size = 0
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise ValueError(f"LocalUpload file changed kind: {path}.")
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open("xb") as output:
            while True:
                chunk = os.read(descriptor, 1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
                size += len(chunk)
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    final = path.stat(follow_symlinks=False)
    identity = (
        before.st_dev,
        before.st_ino,
        before.st_mode,
        before.st_size,
        before.st_mtime_ns,
    )
    if identity != (
        opened.st_dev,
        opened.st_ino,
        opened.st_mode,
        opened.st_size,
        opened.st_mtime_ns,
    ) or identity != (
        after.st_dev,
        after.st_ino,
        after.st_mode,
        after.st_size,
        after.st_mtime_ns,
    ) or identity != (
        final.st_dev,
        final.st_ino,
        final.st_mode,
        final.st_size,
        final.st_mtime_ns,
    ):
        raise ValueError(f"LocalUpload changed while it was packaged: {path}.")
    return {
        "digest": f"sha256:{digest.hexdigest()}",
        "kind": "file",
        "path": relative,
        "size": size,
    }


def _walk_upload(source: Path, destination: Path) -> tuple[str, list[dict[str, Any]]]:
    if source.is_symlink():
        raise ValueError("LocalUpload symlinks are forbidden.")
    mode = source.stat(follow_symlinks=False).st_mode
    root_name = _safe_basename(source)
    destination_root = destination / root_name
    entries: list[dict[str, Any]] = []
    collisions: set[str] = set()
    total = 0

    if stat.S_ISREG(mode):
        destination.mkdir(parents=True, exist_ok=True)
        entry = _copy_entry(source, destination_root, root_name)
        return "file", [entry]
    if not stat.S_ISDIR(mode):
        raise ValueError("LocalUpload must name a regular file or directory.")
    destination_root.mkdir(parents=True)
    for current, directory_names, file_names in os.walk(
        source,
        topdown=True,
        followlinks=False,
    ):
        current_path = Path(current)
        names = sorted(directory_names + file_names)
        for name in names:
            normalized = unicodedata.normalize("NFC", name)
            relative_source = (current_path / name).relative_to(source)
            normalized_parts = tuple(
                unicodedata.normalize("NFC", part)
                for part in relative_source.parts
            )
            relative = PurePosixPath(root_name, *normalized_parts)
            if (
                normalized in {"", ".", ".."}
                or len(relative.parts) > MAX_UPLOAD_DEPTH
                or any(character in normalized for character in ("\x00", "\n", "\r"))
            ):
                raise ValueError("LocalUpload contains an unsafe path name.")
            collision_key = str(relative).casefold()
            if collision_key in collisions:
                raise ValueError("LocalUpload contains a Unicode or case collision.")
            collisions.add(collision_key)
            child = current_path / name
            child_mode = child.stat(follow_symlinks=False).st_mode
            if stat.S_ISLNK(child_mode):
                raise ValueError("LocalUpload symlinks are forbidden.")
            target = destination / Path(*relative.parts)
            if stat.S_ISDIR(child_mode):
                target.mkdir()
                entries.append(
                    {
                        "digest": _EMPTY_DIGEST,
                        "kind": "directory",
                        "path": str(relative),
                        "size": 0,
                    }
                )
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            entry = _copy_entry(child, target, str(relative))
            entries.append(entry)
            total += entry["size"]
            if len(entries) > MAX_UPLOAD_FILES or total > MAX_UPLOAD_BYTES:
                raise ValueError("LocalUpload exceeds the transport limits.")
    _verify_source_tree(source, root_name=root_name, expected=entries)
    return "directory", entries


def _verify_source_tree(
    source: Path,
    *,
    root_name: str,
    expected: list[dict[str, Any]],
) -> None:
    observed: list[dict[str, Any]] = []
    collisions: set[str] = set()
    for path in sorted(source.rglob("*")):
        if path.is_symlink():
            raise ValueError("LocalUpload symlinks are forbidden.")
        relative = PurePosixPath(
            root_name,
            *(
                unicodedata.normalize("NFC", part)
                for part in path.relative_to(source).parts
            ),
        )
        collision_key = str(relative).casefold()
        if collision_key in collisions:
            raise ValueError("LocalUpload contains a Unicode or case collision.")
        collisions.add(collision_key)
        mode = path.stat(follow_symlinks=False).st_mode
        if stat.S_ISDIR(mode):
            observed.append(
                {
                    "digest": _EMPTY_DIGEST,
                    "kind": "directory",
                    "path": str(relative),
                    "size": 0,
                }
            )
        elif stat.S_ISREG(mode):
            observed.append(
                {
                    "digest": file_sha256(path),
                    "kind": "file",
                    "path": str(relative),
                    "size": path.stat().st_size,
                }
            )
        else:
            raise ValueError("LocalUpload contains a special file.")
    ordered_expected = sorted(expected, key=lambda item: item["path"])
    ordered_observed = sorted(observed, key=lambda item: item["path"])
    if ordered_observed != ordered_expected:
        raise ValueError("LocalUpload changed while it was packaged.")


def _dataframe_paths_are_cluster_paths(frame: pd.DataFrame) -> None:
    for value in frame.to_numpy(dtype=object).flat:
        if isinstance(value, LocalUpload):
            raise TypeError("LocalUpload values are forbidden inside DataFrames.")
        if not isinstance(value, Path):
            continue
        encoded = value.as_posix()
        candidate = PurePosixPath(encoded)
        if (
            not candidate.is_absolute()
            or encoded.startswith("//")
            or str(candidate) != encoded
            or any(part in {"", ".", ".."} for part in candidate.parts[1:])
        ):
            raise ValueError(
                "Typed DataFrame Path cells must be normalized absolute "
                "cluster paths."
            )


def _cluster_path_string(value: Path | str, *, field: str) -> str:
    encoded = Path(value).as_posix()
    pure = PurePosixPath(encoded)
    if (
        not pure.is_absolute()
        or encoded.startswith("//")
        or str(pure) != encoded
        or any(part in {"", ".", ".."} for part in pure.parts[1:])
    ):
        raise ValueError(f"{field} must be a normalized absolute cluster path.")
    return encoded


def _manifest(root: Path) -> dict[str, Any]:
    entries = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ValueError("Submission bundle symlinks are forbidden.")
        relative = path.relative_to(root).as_posix()
        if path.is_dir():
            entries.append(
                {
                    "digest": _EMPTY_DIGEST,
                    "kind": "directory",
                    "path": relative,
                    "size": 0,
                }
            )
            continue
        if not path.is_file():
            raise ValueError("Submission bundle contains a special file.")
        entries.append(
            {
                "digest": file_sha256(path),
                "kind": "file",
                "path": relative,
                "size": path.stat().st_size,
            }
        )
    body = {
        "entries": entries,
        "root_name": "submission",
        "schema": MANIFEST_SCHEMA,
    }
    body["digest"] = (
        f"sha256:{hashlib.sha256(canonical_json_bytes(body)).hexdigest()}"
    )
    return body


@contextmanager
def prepare_cluster_bundle(
    workflow: Workflow,
    *,
    inputs: Mapping[str, Any] | None,
    targets: Sequence[str] | None,
    parsl_config: ParslConfigRef,
    executor_bindings: Mapping[str, ExecutorBinding],
    node_routes: Mapping[str, str] | None,
    environment_routes: Mapping[str, str] | None,
    shared_runtime_root: Path | str | None,
    task_policy: ParslTaskPolicy | None,
    launch: PSIJLaunchConfig,
) -> Iterator[PreparedClusterBundle]:
    """Build one private, self-contained laptop-to-cluster request bundle."""
    if targets is not None and inputs is not None:
        raise ValueError("inputs and targets are mutually exclusive.")
    temporary = tempfile.TemporaryDirectory(prefix="bioimageflow-cluster-submit-")
    root = Path(temporary.name)
    try:
        supplied = dict(inputs or {})
        ports = {port.name: port for port in workflow._interface_inputs.values()}
        if unknown := set(supplied) - set(ports):
            raise ValueError(f"Unknown workflow input(s): {sorted(unknown)}.")
        encoded_inputs = []
        for position, (name, value) in enumerate(supplied.items()):
            port = ports[name]
            if isinstance(value, LocalUpload):
                if port.kind != "field" or not is_path_type(port.annotation):
                    raise TypeError(
                        f"LocalUpload is not allowed for workflow input {name!r}."
                    )
                upload_root = root / "uploads" / str(position)
                kind, entries = _walk_upload(value.path, upload_root)
                encoded_inputs.append(
                    {
                        "kind": "local_upload",
                        "name": name,
                        "root_kind": kind,
                        "root_name": _safe_basename(value.path),
                        "tree": entries,
                        "upload_path": f"uploads/{position}",
                    }
                )
            elif port.kind == "dataframe":
                if not isinstance(value, pd.DataFrame):
                    raise TypeError(f"DataFrame input {name!r} requires a DataFrame.")
                _dataframe_paths_are_cluster_paths(value)
                relative = f"dataframes/{position}.parquet"
                metadata = write_dataframe_transport(
                    value,
                    root / relative,
                    preserve_paths=True,
                )
                encoded_inputs.append(
                    {
                        "kind": "dataframe",
                        "metadata": metadata,
                        "name": name,
                        "path": relative,
                    }
                )
            else:
                if isinstance(value, pd.DataFrame):
                    raise TypeError(f"Field input {name!r} cannot be a DataFrame.")
                encoded_inputs.append(
                    {
                        "kind": "constant",
                        "name": name,
                        "value": encode_cluster_typed_constant(value),
                    }
                )
        bindings = _normalize_bindings(executor_bindings)
        labels = frozenset(bindings)
        storage_path = _cluster_path_string(
            workflow.storage_path,
            field="Workflow.storage_path",
        )
        request_value = {
            "environment_routes": _normalize_routes(
                environment_routes,
                labels=labels,
                field="environment_routes",
            ),
            "executor_bindings": bindings,
            "inputs": encoded_inputs,
            "launch": launch.to_dict(),
            "node_routes": _normalize_routes(
                node_routes,
                labels=labels,
                field="node_routes",
            ),
            "parsl_config": parsl_config.to_dict(),
            "schema": BUNDLE_SCHEMA,
            "shared_runtime_root": (
                None
                if shared_runtime_root is None
                else _cluster_path_string(
                    shared_runtime_root,
                    field="shared_runtime_root",
                )
            ),
            "storage_path": storage_path,
            "targets": None if targets is None else list(targets),
            "task_policy": (task_policy or ParslTaskPolicy()).to_dict(),
            "workflow": serialize_workflow_payload(workflow),
        }
        (root / "request.json").write_bytes(canonical_json_bytes(request_value))
        manifest = _manifest(root)
        yield PreparedClusterBundle(root=root, manifest=manifest)
    finally:
        temporary.cleanup()
