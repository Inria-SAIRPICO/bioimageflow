"""Cluster-side validation and idempotent launcher submission."""

from __future__ import annotations

import json
import unicodedata
from pathlib import Path, PurePosixPath
from typing import Any

from bioimageflow.parsl import ExecutorBinding, ParslTaskPolicy
from bioimageflow.storage.dataframe_transport import (
    file_sha256,
    read_dataframe_transport,
)
from bioimageflow.validation import is_path_type

from .cluster_bundle import BUNDLE_SCHEMA
from .configuration import import_config_factory, verify_secret_references
from .errors import BackendNotSupportedError, PSIJSubmissionUncertainError
from .cluster_protocol import (
    MAX_REQUEST_BYTES,
    ClusterProtocolFailure,
    _check_tree,
)
from .cluster_upload import _ensure_root, _verify_tree, validate_manifest
from .inputs import decode_cluster_typed_constant
from .payload import load_workflow_payload
from .repository import (
    LauncherRepository,
    RunNotFoundError,
    _CrossProcessLock,
    _atomic_write_json,
    _read_json,
)
from .submission import _launch_prepared_control, _submit_workflow
from .types import PSIJLaunchConfig, ParslConfigRef


def _absolute_cluster_path(value: Any, *, field: str) -> Path:
    if type(value) is not str:
        raise ClusterProtocolFailure(
            "invalid-remote-submission",
            f"{field} must be a normalized absolute POSIX path.",
        )
    pure = PurePosixPath(value)
    if (
        not pure.is_absolute()
        or value.startswith("//")
        or str(pure) != value
        or any(part in {"", ".", ".."} for part in pure.parts[1:])
    ):
        raise ClusterProtocolFailure(
            "invalid-remote-submission",
            f"{field} must be a normalized absolute POSIX path.",
        )
    return Path(value)


def _is_within(candidate: Path, root: Path) -> bool:
    try:
        candidate.resolve(strict=False).relative_to(root.resolve(strict=False))
    except ValueError:
        return False
    return True


def _load_request(object_root: Path) -> dict[str, Any]:
    request_path = object_root / "request.json"
    if request_path.is_symlink() or not request_path.is_file():
        raise ClusterProtocolFailure(
            "invalid-remote-submission",
            "Submission bundle request.json is missing or unsafe.",
        )
    if request_path.stat().st_size > MAX_REQUEST_BYTES:
        raise ClusterProtocolFailure(
            "invalid-remote-submission",
            "Submission bundle request exceeds its byte limit.",
        )

    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, item in pairs:
            if key in result:
                raise ClusterProtocolFailure(
                    "invalid-remote-submission",
                    "Submission bundle contains a duplicate JSON key.",
                )
            result[key] = item
        return result

    def reject_nonfinite(value: str) -> None:
        raise ClusterProtocolFailure(
            "invalid-remote-submission",
            f"Submission bundle contains non-finite JSON {value!r}.",
        )

    try:
        value = json.loads(
            request_path.read_text(encoding="utf-8"),
            object_pairs_hook=reject_duplicates,
            parse_constant=reject_nonfinite,
        )
    except ClusterProtocolFailure:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ClusterProtocolFailure(
            "invalid-remote-submission",
            "Submission bundle request.json is malformed.",
        ) from exc
    _check_tree(value)
    expected = {
        "environment_routes",
        "executor_bindings",
        "inputs",
        "launch",
        "node_routes",
        "parsl_config",
        "schema",
        "shared_runtime_root",
        "storage_path",
        "targets",
        "task_policy",
        "workflow",
    }
    if type(value) is not dict or set(value) != expected or value["schema"] != BUNDLE_SCHEMA:
        raise ClusterProtocolFailure(
            "invalid-remote-submission",
            "Submission bundle request schema is invalid.",
        )
    return value


def _load_inputs(
    object_root: Path,
    workflow: Any,
    value: Any,
) -> dict[str, Any]:
    if type(value) is not list:
        raise ClusterProtocolFailure(
            "invalid-remote-submission",
            "Submission inputs must be a list.",
        )
    result: dict[str, Any] = {}
    ports = {port.name: port for port in workflow._interface_inputs.values()}
    for entry in value:
        if type(entry) is not dict or type(entry.get("name")) is not str:
            raise ClusterProtocolFailure(
                "invalid-remote-submission",
                "Submission input entry is malformed.",
            )
        name = entry["name"]
        port = ports.get(name)
        if port is None:
            raise ClusterProtocolFailure(
                "invalid-remote-submission",
                "Submission input names an unknown workflow root field.",
            )
        if name in result:
            raise ClusterProtocolFailure(
                "invalid-remote-submission",
                "Submission input names must be unique.",
            )
        kind = entry.get("kind")
        try:
            if kind == "constant" and set(entry) == {"kind", "name", "value"}:
                result[name] = decode_cluster_typed_constant(entry["value"])
            elif kind == "dataframe" and set(entry) == {
                "kind",
                "metadata",
                "name",
                "path",
            }:
                relative = PurePosixPath(entry["path"])
                if relative.is_absolute() or ".." in relative.parts:
                    raise ValueError("unsafe dataframe path")
                result[name] = read_dataframe_transport(
                    object_root / Path(*relative.parts),
                    entry["metadata"],
                    preserve_paths=True,
                )
            elif kind == "local_upload" and set(entry) == {
                "kind",
                "name",
                "root_kind",
                "root_name",
                "tree",
                "upload_path",
            }:
                schema_type = (
                    port.schema.get("type")
                    if type(port.schema) is dict
                    else None
                )
                if port.kind != "field" or not (
                    is_path_type(port.annotation)
                    or schema_type in {"ImageFile", "Path"}
                ):
                    raise ValueError("upload marker is not in a path-like root field")
                base = PurePosixPath(entry["upload_path"])
                if (
                    base.is_absolute()
                    or len(base.parts) != 2
                    or base.parts[0] != "uploads"
                    or not base.parts[1].isdigit()
                ):
                    raise ValueError("unsafe upload path")
                root_name = entry["root_name"]
                if (
                    type(root_name) is not str
                    or not root_name
                    or unicodedata.normalize("NFC", root_name) != root_name
                    or root_name in {".", ".."}
                    or "/" in root_name
                    or "\\" in root_name
                    or any(
                        character in root_name
                        for character in ("\x00", "\n", "\r")
                    )
                    or entry["root_kind"] not in {"directory", "file"}
                    or type(entry["tree"]) is not list
                ):
                    raise ValueError("invalid upload marker")
                path = object_root / Path(*base.parts) / root_name
                if path.is_symlink() or not path.exists():
                    raise ValueError("installed upload is missing")
                if entry["root_kind"] == "file" and not path.is_file():
                    raise ValueError("installed upload kind mismatch")
                if entry["root_kind"] == "directory" and not path.is_dir():
                    raise ValueError("installed upload kind mismatch")
                _validate_upload_tree(
                    path,
                    root_name=root_name,
                    root_kind=entry["root_kind"],
                    declared=entry["tree"],
                )
                result[name] = path
            else:
                raise ValueError("unknown input kind or fields")
        except (OSError, TypeError, ValueError) as exc:
            raise ClusterProtocolFailure(
                "invalid-remote-submission",
                f"Submission input {name!r} failed validation.",
            ) from exc
    return result


def _validate_upload_tree(
    root: Path,
    *,
    root_name: str,
    root_kind: str,
    declared: list[Any],
) -> None:
    empty_digest = (
        "sha256:"
        "e3b0c44298fc1c149afbf4c8996fb924"
        "27ae41e4649b934ca495991b7852b855"
    )
    observed: list[dict[str, Any]] = []
    paths = [root] if root_kind == "file" else sorted(root.rglob("*"))
    for path in paths:
        relative = (
            root_name
            if path == root
            else PurePosixPath(root_name, *path.relative_to(root).parts).as_posix()
        )
        if path.is_dir():
            observed.append(
                {
                    "digest": empty_digest,
                    "kind": "directory",
                    "path": relative,
                    "size": 0,
                }
            )
        elif path.is_file():
            observed.append(
                {
                    "digest": file_sha256(path),
                    "kind": "file",
                    "path": relative,
                    "size": path.stat().st_size,
                }
            )
        else:
            raise ValueError("installed upload contains a special file")
    if root_kind == "directory":
        observed = [item for item in observed if item["path"] != root_name]
    if (
        any(type(item) is not dict for item in declared)
        or sorted(declared, key=lambda item: str(item.get("path")))
        != sorted(observed, key=lambda item: str(item["path"]))
    ):
        raise ValueError("installed upload tree does not match its marker")


def submit_bundle(
    staging_root: Path,
    request_id: str,
    request_digest: str,
    object_path: Any,
    manifest: Any,
) -> dict[str, Any]:
    """Bind one request to one run ID and dispatch through the Phase 1b seam."""
    _ensure_root(staging_root)
    validated_manifest = validate_manifest(manifest)
    expected_object = (
        staging_root
        / "objects"
        / "sha256"
        / validated_manifest["digest"].removeprefix("sha256:")
        / "submission"
    )
    if type(object_path) is not str or Path(object_path) != expected_object:
        raise ClusterProtocolFailure(
            "invalid-object-path",
            "submit must name the committed content-addressed object.",
        )
    _verify_tree(expected_object, validated_manifest)
    request_value = _load_request(expected_object)
    storage_path = _absolute_cluster_path(
        request_value["storage_path"],
        field="storage_path",
    )
    if _is_within(staging_root, storage_path) or _is_within(storage_path, staging_root):
        raise ClusterProtocolFailure(
            "overlapping-storage-roots",
            "Transport staging_root and Workflow.storage_path must be disjoint.",
        )
    try:
        workflow = load_workflow_payload(
            request_value["workflow"],
            storage_path=storage_path,
        )
        bindings = {
            label: ExecutorBinding.from_dict(value)
            for label, value in request_value["executor_bindings"].items()
        }
        inputs = _load_inputs(
            expected_object,
            workflow,
            request_value["inputs"],
        )
        parsl_config = ParslConfigRef.from_dict(request_value["parsl_config"])
        task_policy = ParslTaskPolicy.from_dict(request_value["task_policy"])
        launch = PSIJLaunchConfig.from_dict(request_value["launch"])
        import_config_factory(parsl_config.factory)
        verify_secret_references(parsl_config)
    except ClusterProtocolFailure:
        raise
    except Exception as exc:
        raise ClusterProtocolFailure(
            "invalid-remote-submission",
            f"Cluster submission validation failed: {type(exc).__name__}.",
        ) from exc
    receipt_dir = staging_root / "receipts" / "submit"
    receipt_dir.mkdir(mode=0o700, exist_ok=True)
    receipt_path = receipt_dir / f"{request_id}.json"
    dispatch_path = receipt_dir / f"{request_id}.dispatched.json"
    lock_path = staging_root / "locks" / f"submit-{request_id}.lock"
    with _CrossProcessLock(lock_path):
        if receipt_path.exists():
            receipt = _read_json(receipt_path)
            if receipt.get("request_digest") != request_digest:
                raise ClusterProtocolFailure(
                    "duplicate-request-conflict",
                    "request_id was already used with different arguments.",
                )
            run_id = receipt.get("run_id")
            if type(run_id) is not str:
                raise ClusterProtocolFailure(
                    "corrupt-receipt",
                    "Submit receipt is malformed.",
                )
        else:
            run_id = LauncherRepository(storage_path).new_run_id()
            _atomic_write_json(
                receipt_path,
                {
                    "bundle_digest": validated_manifest["digest"],
                    "request_digest": request_digest,
                    "request_id": request_id,
                    "run_id": run_id,
                    "schema": "bioimageflow.cluster.submit_receipt.v1",
                },
            )

        repository = LauncherRepository(storage_path)
        try:
            control = repository.open(run_id)
        except RunNotFoundError:
            try:
                run = _submit_workflow(
                    workflow,
                    inputs=inputs if request_value["targets"] is None else None,
                    targets=request_value["targets"],
                    parsl_config=parsl_config,
                    executor_bindings=bindings,
                    node_routes=request_value["node_routes"],
                    environment_routes=request_value["environment_routes"],
                    shared_runtime_root=request_value["shared_runtime_root"],
                    task_policy=task_policy,
                    launch=launch,
                    preallocated_run_id=run_id,
                    preserve_cluster_paths=True,
                )
            except ClusterProtocolFailure:
                raise
            except PSIJSubmissionUncertainError as exc:
                raise ClusterProtocolFailure(
                    "psij-submission-uncertain",
                    "PSI/J submission outcome is uncertain; retry will not submit again.",
                ) from exc
            except BackendNotSupportedError as exc:
                raise ClusterProtocolFailure(
                    "psij-unavailable",
                    "Requested PSI/J executor is unavailable in the cluster environment.",
                ) from exc
            except Exception as exc:
                raise ClusterProtocolFailure(
                    "remote-submission-failed",
                    f"Cluster submission failed: {type(exc).__name__}.",
                ) from exc
            if run.id != run_id:
                raise ClusterProtocolFailure(
                    "run-binding-failed",
                    "Launcher did not retain the preallocated run ID.",
                )
            _atomic_write_json(
                dispatch_path,
                {
                    "request_digest": request_digest,
                    "run_id": run_id,
                    "schema": "bioimageflow.cluster.submit_dispatch.v1",
                },
            )
        else:
            if dispatch_path.exists():
                dispatched = _read_json(dispatch_path)
                if dispatched != {
                    "request_digest": request_digest,
                    "run_id": run_id,
                    "schema": "bioimageflow.cluster.submit_dispatch.v1",
                }:
                    raise ClusterProtocolFailure(
                        "corrupt-receipt",
                        "Submit dispatch receipt is malformed.",
                    )
            elif control.read_status()["state"] == "prepared":
                try:
                    _launch_prepared_control(
                        control,
                        launch,
                        parsl_config=parsl_config,
                    )
                except PSIJSubmissionUncertainError as exc:
                    raise ClusterProtocolFailure(
                        "psij-submission-uncertain",
                        "PSI/J submission outcome is uncertain; retry will not submit again.",
                    ) from exc
                except BackendNotSupportedError as exc:
                    raise ClusterProtocolFailure(
                        "psij-unavailable",
                        "Requested PSI/J executor is unavailable in the cluster environment.",
                    ) from exc
                _atomic_write_json(
                    dispatch_path,
                    {
                        "request_digest": request_digest,
                        "run_id": run_id,
                        "schema": "bioimageflow.cluster.submit_dispatch.v1",
                    },
                )
        return {"run_id": run_id, "storage_path": storage_path.as_posix()}
