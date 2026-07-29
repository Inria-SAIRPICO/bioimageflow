"""Immutable transport bundles for successful submitted-workflow returns."""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
import uuid
from pathlib import Path
from typing import Any, Mapping

from bioimageflow.storage import Storage, asset_digest_and_size, canonical_json_bytes
from bioimageflow.storage.dataframe_transport import file_sha256

from .cluster_protocol import ClusterProtocolFailure
from .cluster_upload import (
    _ensure_confined_directory,
    _ensure_root,
    _make_read_only,
    _receipt,
    normalized_root,
)
from .errors import WorkflowRunResultUnavailableError
from .remote_control import _open
from .repository import _CrossProcessLock, _atomic_write_json, _sync_directory
from .returns import load_return_manifest

RESULT_BUNDLE_SCHEMA = "bioimageflow.cluster.result-bundle.v1"
_EMPTY_DIGEST = f"sha256:{hashlib.sha256(b'').hexdigest()}"


def _disjoint(root: Path, storage: Path) -> None:
    resolved_root = root.resolve(strict=False)
    resolved_storage = storage.resolve(strict=False)
    if (
        resolved_root == resolved_storage
        or resolved_root in resolved_storage.parents
        or resolved_storage in resolved_root.parents
    ):
        raise ClusterProtocolFailure(
            "unsafe-staging-root",
            "Transport staging and workflow storage must be disjoint.",
        )


def _copy_regular(source: Path, destination: Path) -> None:
    before = source.lstat()
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        raise ClusterProtocolFailure(
            "result-unavailable",
            "A required return asset is not a regular file.",
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(source, flags)
    try:
        with os.fdopen(descriptor, "rb", closefd=False) as reader:
            with destination.open("xb") as writer:
                shutil.copyfileobj(reader, writer)
                writer.flush()
                os.fsync(writer.fileno())
        os.close(descriptor)
        descriptor = -1
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    after = source.lstat()
    if (
        after.st_dev != before.st_dev
        or after.st_ino != before.st_ino
        or after.st_mtime_ns != before.st_mtime_ns
        or after.st_size != before.st_size
        or after.st_size != destination.stat().st_size
    ):
        raise ClusterProtocolFailure(
            "result-mutated",
            "A return asset changed while it was copied.",
        )


def _copy_asset(source: Path, destination: Path) -> None:
    mode = source.lstat().st_mode
    if stat.S_ISLNK(mode):
        raise ClusterProtocolFailure(
            "result-unavailable",
            "Symlink return assets are forbidden.",
        )
    if stat.S_ISREG(mode):
        _copy_regular(source, destination)
        return
    if not stat.S_ISDIR(mode):
        raise ClusterProtocolFailure(
            "result-unavailable",
            "Special-file return assets are forbidden.",
        )
    destination.mkdir(parents=True)
    for child in sorted(source.iterdir(), key=lambda item: item.name):
        _copy_asset(child, destination / child.name)


def _entries(root: Path) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        relative = path.relative_to(root).as_posix()
        mode = path.lstat().st_mode
        if stat.S_ISLNK(mode):
            raise ClusterProtocolFailure(
                "result-integrity",
                "Result bundle contains a symlink.",
            )
        if stat.S_ISDIR(mode):
            entries.append(
                {
                    "digest": _EMPTY_DIGEST,
                    "kind": "directory",
                    "path": relative,
                    "size": 0,
                }
            )
        elif stat.S_ISREG(mode):
            entries.append(
                {
                    "digest": file_sha256(path),
                    "kind": "file",
                    "path": relative,
                    "size": path.stat().st_size,
                }
            )
        else:
            raise ClusterProtocolFailure(
                "result-integrity",
                "Result bundle contains a special file.",
            )
    return entries


def _remove_candidate(path: Path) -> None:
    for child in path.rglob("*"):
        try:
            os.chmod(child, 0o700 if child.is_dir() else 0o600)
        except OSError:
            pass
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass
    shutil.rmtree(path)


def _verify(
    root: Path,
    manifest: Mapping[str, Any],
    *,
    require_read_only: bool = False,
) -> None:
    if root.is_symlink() or not root.is_dir():
        raise ClusterProtocolFailure(
            "result-integrity",
            "Result download object is unavailable.",
        )
    actual = _entries(root)
    if require_read_only and any(
        path.lstat().st_mode & 0o222 for path in (root, *root.rglob("*"))
    ):
        raise ClusterProtocolFailure(
            "result-integrity",
            "Result download object is not immutable.",
        )
    actual = [entry for entry in actual if entry["path"] != "manifest.json"]
    if actual != manifest["entries"]:
        raise ClusterProtocolFailure(
            "result-integrity",
            "Result download object no longer matches its manifest.",
        )
    body = {key: manifest[key] for key in manifest if key != "digest"}
    expected = f"sha256:{hashlib.sha256(canonical_json_bytes(body)).hexdigest()}"
    if manifest.get("digest") != expected:
        raise ClusterProtocolFailure(
            "result-integrity",
            "Result download manifest digest is invalid.",
        )


def _build_candidate(run: Any, candidate: Path) -> dict[str, Any]:
    storage = Storage(run._storage_path)
    try:
        return_manifest = load_return_manifest(
            run.control_dir,
            expected_run_id=run.id,
            storage_path=run._storage_path,
        )
    except WorkflowRunResultUnavailableError as exc:
        raise ClusterProtocolFailure(
            "result-unavailable",
            "The successful workflow return is unavailable.",
        ) from exc
    record_assets: list[dict[str, Any]] = []
    for frame in return_manifest["frames"]:
        relative = Path(frame["path"])
        _copy_regular(run.control_dir / relative, candidate / relative)
    copied_returns: set[str] = set()
    for position, locator in enumerate(return_manifest["locators"]):
        if locator["kind"] == "return_asset":
            relative = locator["path"]
            if relative not in copied_returns:
                _copy_asset(
                    run.control_dir / Path(relative),
                    candidate / Path(relative),
                )
                _size, digest = asset_digest_and_size(candidate / Path(relative))
                if digest != locator["digest"]:
                    raise ClusterProtocolFailure(
                        "result-mutated",
                        "A return asset changed while it was bundled.",
                    )
                copied_returns.add(relative)
        elif locator["kind"] == "record_asset":
            try:
                source = storage.resolve_record_asset(
                    locator["result_key"],
                    locator["record_id"],
                    locator["asset_path"],
                )
            except Exception as exc:
                raise ClusterProtocolFailure(
                    "result-unavailable",
                    "An immutable record required by the result is unavailable.",
                ) from exc
            relative = f"records/locator_{position:06d}/{source.name}"
            _copy_asset(source, candidate / relative)
            _size, digest = asset_digest_and_size(candidate / relative)
            if digest != locator["digest"]:
                raise ClusterProtocolFailure(
                    "result-mutated",
                    "An immutable record asset changed while it was bundled.",
                )
            record_assets.append(
                {"locator_index": position, "path": relative}
            )
    manifest: dict[str, Any] = {
        "entries": _entries(candidate),
        "record_assets": record_assets,
        "return_manifest": return_manifest,
        "run_id": run.id,
        "schema": RESULT_BUNDLE_SCHEMA,
        "storage_path": run._storage_path.as_posix(),
    }
    manifest["digest"] = (
        f"sha256:{hashlib.sha256(canonical_json_bytes(manifest)).hexdigest()}"
    )
    _atomic_write_json(candidate / "manifest.json", manifest)
    _verify(candidate, manifest)
    return manifest


def prepare_result(
    staging_root: Any,
    storage_path: Any,
    run_id: Any,
    request_id: str,
    request_digest: str,
) -> dict[str, Any]:
    """Create or reuse a validated immutable result download object."""
    root = normalized_root(staging_root)
    storage = normalized_root(storage_path)
    _disjoint(root, storage)
    _ensure_root(root)

    def mutate() -> dict[str, Any]:
        run = _open(storage_path, run_id)
        run.refresh()
        if run.status != "succeeded":
            raise ClusterProtocolFailure(
                f"workflow-{run.status}",
                f"Workflow run is {run.status!r}, not succeeded.",
            )
        candidate_root = root / ".result-partial"
        _ensure_confined_directory(candidate_root, root=root)
        candidate = candidate_root / str(uuid.uuid4())
        candidate.mkdir(mode=0o700)
        try:
            manifest = _build_candidate(run, candidate)
            digest = manifest["digest"].removeprefix("sha256:")
            with _CrossProcessLock(root / "locks" / f"result-{digest}.lock"):
                parent = root / "results" / "sha256" / digest
                _ensure_confined_directory(parent, root=root)
                installed = parent / "download"
                if installed.exists():
                    persisted = _read_manifest(installed)
                    _verify(installed, persisted, require_read_only=True)
                    if persisted != manifest:
                        raise ClusterProtocolFailure(
                            "result-integrity",
                            "A different result owns this download identity.",
                        )
                    _remove_candidate(candidate)
                else:
                    os.rename(candidate, installed)
                    _make_read_only(installed)
                    _verify(installed, manifest, require_read_only=True)
                    _sync_directory(parent)
            return {
                "bundle_digest": manifest["digest"],
                "remote_root": installed.as_posix(),
                "run_id": run.id,
                "storage_path": run._storage_path.as_posix(),
            }
        finally:
            if candidate.exists() and not candidate.is_symlink():
                _remove_candidate(candidate)

    return _receipt(root, "prepare-result", request_id, request_digest, mutate)


def _read_manifest(root: Path) -> dict[str, Any]:
    import json

    path = root / "manifest.json"
    if path.is_symlink() or not path.is_file():
        raise ClusterProtocolFailure(
            "result-integrity",
            "Result download manifest is unavailable.",
        )
    try:
        value = json.loads(path.read_text())
    except Exception as exc:
        raise ClusterProtocolFailure(
            "result-integrity",
            "Result download manifest is malformed.",
        ) from exc
    if type(value) is not dict:
        raise ClusterProtocolFailure(
            "result-integrity",
            "Result download manifest is malformed.",
        )
    return value
