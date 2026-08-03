"""Validated atomic SFTP materialization of immutable result bundles."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
import unicodedata
import uuid
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

from bioimageflow.storage import canonical_json_bytes, validate_relative_posix_path
from bioimageflow.storage.dataframe_transport import file_sha256

from .cluster_protocol import ClusterProtocolFailure
from .errors import (
    WorkflowResultDestinationError,
    WorkflowRunResultUnavailableError,
)
from .return_schema import validate_return_manifest_structure
from .returns import load_public_return_from_bundle
from .result_bundle import MAX_RESULT_BYTES, MAX_RESULT_DEPTH, MAX_RESULT_ENTRIES
from .ssh import (
    SSHTransportError,
    _sftp_quote,
    _subprocess_environment,
    _timeout_seconds,
)
from .types import SSHSubmissionTransport

_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_EMPTY_DIGEST = f"sha256:{hashlib.sha256(b'').hexdigest()}"
RESULT_BUNDLE_SCHEMA = "bioimageflow.cluster.result-bundle.v1"
MAX_RESULT_MANIFEST_BYTES = 64 * 1024 * 1024
_LOCAL_EXPORT_RECEIPT_SCHEMA = "bioimageflow.launcher.result-export.v1"


def _validate_destination_parent(destination: Path) -> None:
    parent = destination.parent
    current = Path(parent.anchor)
    for part in parent.parts[1:]:
        current /= part
        try:
            mode = current.lstat().st_mode
        except OSError as exc:
            raise SSHTransportError(
                "unsafe-destination",
                "Every result destination parent must already exist.",
            ) from exc
        if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
            raise SSHTransportError(
                "unsafe-destination",
                "Every result destination parent must be a real directory.",
            )


def _run_sftp(
    transport: SSHSubmissionTransport,
    commands: list[str],
) -> None:
    try:
        completed = subprocess.run(
            [
                "sftp",
                "-b",
                "-",
                "-o",
                "BatchMode=yes",
                "-o",
                f"ConnectTimeout={_timeout_seconds(transport.connect_timeout)}",
                "--",
                transport.host,
            ],
            input=("\n".join(commands) + "\n").encode("utf-8"),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            shell=False,
            timeout=transport.connect_timeout,
            env=_subprocess_environment(),
        )
    except subprocess.TimeoutExpired as exc:
        raise SSHTransportError(
            "sftp-timeout",
            "SFTP result download timed out.",
        ) from exc
    except OSError as exc:
        raise SSHTransportError(
            "sftp-unavailable",
            "System SFTP executable could not be started.",
        ) from exc
    if completed.returncode != 0:
        raise SSHTransportError(
            "sftp-transfer-failed",
            "SFTP result download did not complete.",
        )


def _load_manifest(path: Path) -> dict[str, Any]:
    try:
        if path.is_symlink() or not path.is_file():
            raise ValueError("manifest is not a regular file")
        encoded = path.read_bytes()
        if len(encoded) > MAX_RESULT_MANIFEST_BYTES:
            raise ValueError("manifest exceeds the byte limit")
        value = json.loads(
            encoded.decode("utf-8"),
            object_pairs_hook=_reject_duplicates,
            parse_constant=_reject_nonfinite,
        )
    except Exception as exc:
        raise SSHTransportError(
            "result-integrity",
            "Downloaded result manifest is malformed.",
        ) from exc
    try:
        if type(value) is not dict or set(value) != {
            "digest",
            "entries",
            "record_assets",
            "return_manifest",
            "run_id",
            "schema",
            "storage_path",
        }:
            raise SSHTransportError(
                "result-integrity",
                "Downloaded result manifest schema is invalid.",
            )
        if value["schema"] != RESULT_BUNDLE_SCHEMA:
            raise SSHTransportError(
                "result-integrity",
                "Downloaded result manifest version is unsupported.",
            )
        body = {key: value[key] for key in value if key != "digest"}
        expected = f"sha256:{hashlib.sha256(canonical_json_bytes(body)).hexdigest()}"
        if value["digest"] != expected:
            raise SSHTransportError(
                "result-integrity",
                "Downloaded result manifest digest is invalid.",
            )
        return_manifest = validate_return_manifest_structure(
            value["return_manifest"]
        )
        if (
            type(value["run_id"]) is not str
            or return_manifest["run_id"] != value["run_id"]
            or type(value["storage_path"]) is not str
            or not PurePosixPath(value["storage_path"]).is_absolute()
            or value["storage_path"].startswith("//")
            or str(PurePosixPath(value["storage_path"])) != value["storage_path"]
        ):
            raise SSHTransportError(
                "result-integrity",
                "Downloaded result manifest has inconsistent run binding.",
            )
        return value
    except SSHTransportError:
        raise
    except Exception as exc:
        raise SSHTransportError(
            "result-integrity",
            "Downloaded result manifest structure is invalid.",
        ) from exc


def _reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON key")
        value[key] = item
    return value


def _reject_nonfinite(value: str) -> None:
    raise ValueError(f"non-finite JSON value {value}")


def _validate_entries(value: Any) -> list[dict[str, Any]]:
    if type(value) is not list or len(value) > MAX_RESULT_ENTRIES:
        raise SSHTransportError(
            "result-integrity",
            "Result bundle file list is invalid.",
        )
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    folded: set[str] = set()
    total = 0
    for raw in value:
        if type(raw) is not dict or set(raw) != {"digest", "kind", "path", "size"}:
            raise SSHTransportError(
                "result-integrity",
                "Result bundle entry schema is invalid.",
            )
        try:
            relative = validate_relative_posix_path(raw["path"])
        except (TypeError, ValueError) as exc:
            raise SSHTransportError(
                "result-integrity",
                "Result bundle entry path is unsafe.",
            ) from exc
        if (
            relative == "manifest.json"
            or relative in seen
            or relative.casefold() in folded
            or unicodedata.normalize("NFC", relative) != relative
            or any(character in relative for character in ("\x00", "\n", "\r"))
            or len(PurePosixPath(relative).parts) > MAX_RESULT_DEPTH
            or raw["kind"] not in {"directory", "file"}
            or type(raw["size"]) is not int
            or raw["size"] < 0
            or type(raw["digest"]) is not str
            or _DIGEST.fullmatch(raw["digest"]) is None
        ):
            raise SSHTransportError(
                "result-integrity",
                "Result bundle entry value is invalid.",
            )
        if raw["kind"] == "directory" and (
            raw["size"] != 0 or raw["digest"] != _EMPTY_DIGEST
        ):
            raise SSHTransportError(
                "result-integrity",
                "Result bundle directory entry is invalid.",
            )
        seen.add(relative)
        folded.add(relative.casefold())
        total += raw["size"]
        if total > MAX_RESULT_BYTES:
            raise SSHTransportError(
                "result-integrity",
                "Result bundle exceeds the aggregate byte limit.",
            )
        result.append(dict(raw))
    if [entry["path"] for entry in result] != sorted(seen):
        raise SSHTransportError(
            "result-integrity",
            "Result bundle entries are not in canonical order.",
        )
    return result


def _validate_record_assets(
    value: Any,
    return_manifest: Mapping[str, Any],
    root: Path,
) -> dict[int, Path]:
    if type(value) is not list:
        raise SSHTransportError(
            "result-integrity",
            "Result record-asset map is invalid.",
        )
    result: dict[int, Path] = {}
    for item in value:
        if type(item) is not dict or set(item) != {"locator_index", "path"}:
            raise SSHTransportError(
                "result-integrity",
                "Result record-asset entry is invalid.",
            )
        index = item["locator_index"]
        if (
            type(index) is not int
            or index < 0
            or index >= len(return_manifest["locators"])
            or return_manifest["locators"][index]["kind"] != "record_asset"
            or index in result
        ):
            raise SSHTransportError(
                "result-integrity",
                "Result record-asset locator is invalid.",
            )
        try:
            relative = validate_relative_posix_path(item["path"])
        except (TypeError, ValueError) as exc:
            raise SSHTransportError(
                "result-integrity",
                "Result record-asset path is unsafe.",
            ) from exc
        result[index] = root / relative
    expected = {
        index
        for index, locator in enumerate(return_manifest["locators"])
        if locator["kind"] == "record_asset"
    }
    if set(result) != expected:
        raise SSHTransportError(
            "result-integrity",
            "Result record-asset map is incomplete.",
        )
    return result


def _verify_tree(root: Path, manifest: Mapping[str, Any]) -> dict[int, Path]:
    entries = _validate_entries(manifest["entries"])
    by_path = {entry["path"]: entry for entry in entries}
    actual: set[str] = set()
    for path in root.rglob("*"):
        relative = path.relative_to(root).as_posix()
        if relative == "manifest.json":
            continue
        actual.add(relative)
        mode = path.lstat().st_mode
        entry = by_path.get(relative)
        if entry is None or stat.S_ISLNK(mode):
            raise SSHTransportError(
                "result-integrity",
                "Result bundle contains an unexpected or unsafe path.",
            )
        if entry["kind"] == "directory":
            if not stat.S_ISDIR(mode):
                raise SSHTransportError("result-integrity", "Result directory is invalid.")
        elif (
            not stat.S_ISREG(mode)
            or path.stat().st_size != entry["size"]
            or file_sha256(path) != entry["digest"]
        ):
            raise SSHTransportError("result-integrity", "Result file digest is invalid.")
    expected = {entry["path"] for entry in entries}
    if actual != expected:
        raise SSHTransportError(
            "result-integrity",
            "Result bundle has missing or extra paths.",
        )
    frame_paths = {frame["path"] for frame in manifest["return_manifest"]["frames"]}
    if not frame_paths.issubset(expected):
        raise SSHTransportError(
            "result-integrity",
            "Result bundle is missing a declared return frame.",
        )
    record_assets = _validate_record_assets(
        manifest["record_assets"],
        manifest["return_manifest"],
        root,
    )
    if any(path.relative_to(root).as_posix() not in expected for path in record_assets.values()):
        raise SSHTransportError(
            "result-integrity",
            "Result record-asset map names an undeclared path.",
        )
    return record_assets


def _validate_remote_root(
    transport: SSHSubmissionTransport,
    remote_root: str,
    digest: str,
) -> PurePosixPath:
    expected = (
        transport.staging_root
        / "results"
        / "sha256"
        / digest.removeprefix("sha256:")
        / "download"
    )
    if remote_root != str(expected):
        raise SSHTransportError(
            "remote-protocol",
            "Cluster returned an unsafe result download path.",
        )
    return expected


def download_result(
    transport: SSHSubmissionTransport,
    response: Mapping[str, Any],
    destination: Path,
) -> Any:
    """Download, validate, atomically install, and rehydrate one result."""
    digest = response["bundle_digest"]
    remote = _validate_remote_root(transport, response["remote_root"], digest)
    destination = Path(destination).absolute()
    parent = destination.parent
    current = Path(parent.anchor)
    unsafe_parent = False
    for part in parent.parts[1:]:
        current /= part
        try:
            mode = current.lstat().st_mode
        except OSError:
            unsafe_parent = True
            break
        if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
            unsafe_parent = True
            break
    if unsafe_parent:
        raise SSHTransportError(
            "unsafe-destination",
            "Every result destination parent must be a real non-symlink directory.",
        )
    if destination.exists():
        if destination.is_symlink() or not destination.is_dir():
            raise WorkflowResultDestinationError(
                "Result destination already exists."
            )
        manifest = _load_manifest(destination / "manifest.json")
        if (
            manifest["digest"] != digest
            or manifest["run_id"] != response["run_id"]
            or manifest["storage_path"] != response["storage_path"]
        ):
            raise WorkflowResultDestinationError(
                "Result destination belongs to another bundle."
            )
        assets = _verify_tree(destination, manifest)
        return load_public_return_from_bundle(
            destination,
            manifest["return_manifest"],
            assets,
        )
    candidate = parent / f".{destination.name}.{uuid.uuid4().hex}.partial"
    candidate.mkdir(mode=0o700)
    try:
        _run_sftp(
            transport,
            [
                f"get {_sftp_quote(str(remote / 'manifest.json'))} "
                f"{_sftp_quote(str(candidate / 'manifest.json'))}"
            ],
        )
        manifest = _load_manifest(candidate / "manifest.json")
        if (
            manifest["digest"] != digest
            or manifest["run_id"] != response["run_id"]
            or manifest["storage_path"] != response["storage_path"]
        ):
            raise SSHTransportError(
                "result-integrity",
                "Downloaded result does not match the requested run.",
            )
        entries = _validate_entries(manifest["entries"])
        for entry in entries:
            local = candidate / Path(entry["path"])
            if entry["kind"] == "directory":
                local.mkdir(parents=True, exist_ok=True)
            else:
                local.parent.mkdir(parents=True, exist_ok=True)
        commands = [
            f"get {_sftp_quote(str(remote / entry['path']))} "
            f"{_sftp_quote(str(candidate / entry['path']))}"
            for entry in entries
            if entry["kind"] == "file"
        ]
        if commands:
            _run_sftp(transport, commands)
        assets = _verify_tree(candidate, manifest)
        try:
            os.rename(candidate, destination)
        except FileExistsError:
            raise WorkflowResultDestinationError(
                "Result destination appeared during download."
            )
        return load_public_return_from_bundle(
            destination,
            manifest["return_manifest"],
            {index: destination / path.relative_to(candidate) for index, path in assets.items()},
        )
    except BaseException:
        if candidate.exists() and not candidate.is_symlink():
            import shutil

            shutil.rmtree(candidate)
        raise


def export_local_result(
    run: Any,
    destination: Path,
    *,
    expected_digest: str | None = None,
) -> Any:
    """Build, verify, and atomically install one local result bundle."""
    from .result_bundle import _build_candidate, _remove_candidate

    destination = Path(destination).absolute()
    _validate_destination_parent(destination)
    parent = destination.parent
    retained_digest = _load_local_export_receipt(run)
    if (
        expected_digest is not None
        and retained_digest is not None
        and expected_digest != retained_digest
    ):
        raise SSHTransportError(
            "result-integrity",
            "Expected result identity conflicts with the retained run receipt.",
        )
    expected_digest = expected_digest or retained_digest
    if destination.exists() and expected_digest is not None:
        return _load_existing_local_result(
            run,
            destination,
            expected_digest=expected_digest,
        )
    candidate = parent / f".{destination.name}.{uuid.uuid4().hex}.partial"
    candidate.mkdir(mode=0o700)
    try:
        try:
            manifest = _build_candidate(run, candidate)
        except ClusterProtocolFailure as exc:
            if exc.code == "result-unavailable":
                raise WorkflowRunResultUnavailableError(
                    "The successful workflow return is unavailable.",
                    details={"run_id": run.id},
                ) from exc
            raise SSHTransportError(exc.code, exc.message) from exc
        loaded = _load_manifest(candidate / "manifest.json")
        if loaded != manifest:
            raise SSHTransportError(
                "result-integrity",
                "Built result manifest changed before installation.",
            )
        assets = _verify_tree(candidate, loaded)
        if expected_digest is not None and loaded["digest"] != expected_digest:
            raise SSHTransportError(
                "result-integrity",
                "Built result no longer matches its retained export identity.",
            )
        _install_local_export_receipt(run, loaded["digest"])
        if destination.exists():
            return _load_existing_local_result(
                run,
                destination,
                expected_digest=loaded["digest"],
            )
        try:
            os.rename(candidate, destination)
        except FileExistsError:
            raise WorkflowResultDestinationError(
                "Result destination appeared during export."
            )
        return load_public_return_from_bundle(
            destination,
            loaded["return_manifest"],
            {
                index: destination / path.relative_to(candidate)
                for index, path in assets.items()
            },
        )
    finally:
        if candidate.exists() and not candidate.is_symlink():
            _remove_candidate(candidate)


def _load_existing_local_result(
    run: Any,
    destination: Path,
    *,
    expected_digest: str,
) -> Any:
    """Verify and load an already installed local bundle by expected identity."""
    if destination.is_symlink() or not destination.is_dir():
        raise WorkflowResultDestinationError("Result destination already exists.")
    manifest = _load_manifest(destination / "manifest.json")
    if (
        manifest["digest"] != expected_digest
        or manifest["run_id"] != run.id
        or manifest["storage_path"] != run._storage_path.as_posix()
    ):
        raise WorkflowResultDestinationError(
            "Result destination belongs to another bundle."
        )
    assets = _verify_tree(destination, manifest)
    return load_public_return_from_bundle(
        destination,
        manifest["return_manifest"],
        assets,
    )


def _local_export_receipt_path(run: Any) -> Path:
    return Path(run.control_dir) / "result_export.json"


def _load_local_export_receipt(run: Any) -> str | None:
    path = _local_export_receipt_path(run)
    if not path.exists():
        return None
    try:
        if path.is_symlink() or not path.is_file():
            raise ValueError("receipt is not a regular file")
        value = json.loads(path.read_text())
    except Exception as exc:
        raise SSHTransportError(
            "result-integrity",
            "Retained local result export identity is invalid.",
        ) from exc
    if (
        type(value) is not dict
        or set(value) != {"bundle_digest", "run_id", "schema", "storage_path"}
        or value["schema"] != _LOCAL_EXPORT_RECEIPT_SCHEMA
        or value["run_id"] != run.id
        or value["storage_path"] != run._storage_path.as_posix()
        or type(value["bundle_digest"]) is not str
        or _DIGEST.fullmatch(value["bundle_digest"]) is None
    ):
        raise SSHTransportError(
            "result-integrity",
            "Retained local result export identity is invalid.",
        )
    return str(value["bundle_digest"])


def _install_local_export_receipt(run: Any, digest: str) -> None:
    from .repository import _atomic_create_json

    payload = {
        "bundle_digest": digest,
        "run_id": run.id,
        "schema": _LOCAL_EXPORT_RECEIPT_SCHEMA,
        "storage_path": run._storage_path.as_posix(),
    }
    path = _local_export_receipt_path(run)
    try:
        _atomic_create_json(path, payload)
    except FileExistsError:
        pass
    if _load_local_export_receipt(run) != digest:
        raise SSHTransportError(
            "result-integrity",
            "Retained local result export identity conflicts with this bundle.",
        )
