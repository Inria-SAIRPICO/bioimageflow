"""Two-phase cleanup inventory and application gateway operations."""
# pyright: reportAttributeAccessIssue=false

from __future__ import annotations

import hmac
import os
import stat
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import PurePosixPath
from typing import Any

from ._common import DIGEST_RE, canonical_digest, thaw_json
from ._gateway_archive import (
    _CLEANUP_NAMESPACES,
    _CLEANUP_PLAN_SCHEMA,
    _CLEANUP_REPORT_SCHEMA,
    _MAX_INVENTORY_ENTRIES,
    _canonical_run_id,
    _cleanup_tree_snapshot,
    _open_cleanup_parent,
    _unlink_tree_at,
)
from ._gateway_support import (
    GatewayOperationFailure,
    _exact_arguments,
    _failure,
)


class GatewayCleanupMixin:
    def _retained_run_records(self) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        runs = self.root / "runs"
        for child in sorted(runs.iterdir(), key=lambda item: item.name):
            if child.name.startswith(".cleanup-"):
                continue
            metadata = child.stat(follow_symlinks=False)
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                raise _failure("operation-record-tampered", "The run inventory is unsafe.")
            records.append(self._read_run_record(child.name))
            if len(records) > _MAX_INVENTORY_ENTRIES:
                raise _failure(
                    "resource-limit-exceeded", "The retained run inventory is too large."
                )
        return records

    def _cleanup_reference_revision(
        self, records: list[dict[str, Any]]
    ) -> int:
        transfers: list[dict[str, Any]] = []
        transfer_root = self.root / "transfers"
        for child in sorted(transfer_root.iterdir(), key=lambda item: item.name):
            metadata = child.stat(follow_symlinks=False)
            if stat.S_ISLNK(metadata.st_mode):
                raise _failure("cleanup-conflict", "The transfer inventory is unsafe.")
            identity, total_size = _cleanup_tree_snapshot(
                self.root, child.relative_to(self.root).as_posix()
            )
            transfers.append(
                {
                    "name": child.name,
                    "identity": identity,
                    "size": total_size,
                }
            )
            if len(transfers) > _MAX_INVENTORY_ENTRIES:
                raise _failure(
                    "resource-limit-exceeded", "The transfer inventory is too large."
                )
        digest = canonical_digest(
            {
                "schema": "bioimageflow.cluster.cleanup_reference_revision.v1",
                "runs": [
                    {
                        "run_id": record["run_id"],
                        "revision": record["revision"],
                        "phase": record["phase"],
                        "deployment_id": record["deployment_id"],
                        "object_id": record["object_id"],
                        "plan_digest": record["plan_digest"],
                    }
                    for record in records
                ],
                "transfers": transfers,
            }
        )
        return int(digest[7:22], 16)

    def _run_is_terminal(self, record: Mapping[str, Any]) -> bool:
        if record["phase"] in {"cancelled", "rejected"}:
            return True
        if not record["launcher_bound"]:
            return False
        observation = self.inspect_run({"run_id": record["run_id"]})
        if type(observation.get("terminal")) is not bool:
            raise _failure("cleanup-conflict", "The retained run status is malformed.")
        return observation["terminal"]

    @staticmethod
    def _cleanup_candidate(
        namespace: str,
        identity: str,
        path: str,
        size: int,
        *,
        reference_reasons: tuple[str, ...] = (),
        consequences: tuple[str, ...] = (),
    ) -> dict[str, Any]:
        return {
            "namespace": namespace,
            "identity": identity,
            "path": path,
            "size": size,
            "reference_reasons": list(reference_reasons),
            "consequences": list(consequences),
        }

    def _temporary_cleanup_paths(self, older_than_seconds: int) -> list[str]:
        threshold = datetime.now(timezone.utc).timestamp() - older_than_seconds
        temporary = self.root / "temporary"
        paths: list[str] = []
        for child in sorted(temporary.iterdir(), key=lambda item: item.name):
            if child.name.startswith(".cleanup-") or child.name == "uploads":
                continue
            children = (
                sorted(child.iterdir(), key=lambda item: item.name)
                if child.name in {"deployments", "run-requests", "uploads"}
                and child.is_dir()
                and not child.is_symlink()
                else [child]
            )
            for candidate in children:
                metadata = candidate.stat(follow_symlinks=False)
                if metadata.st_mtime <= threshold:
                    paths.append(candidate.relative_to(self.root).as_posix())
                if len(paths) > _MAX_INVENTORY_ENTRIES:
                    raise _failure(
                        "resource-limit-exceeded", "The temporary inventory is too large."
                    )
        return paths

    def plan_cleanup(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        value = thaw_json(arguments)
        if type(value) is not dict or not set(value).issubset(
            {"namespace", "run_ids", "older_than_seconds"}
        ):
            raise _failure(
                "protocol-incompatible", "Cleanup filters contain unknown fields."
            )
        namespace = value.get("namespace")
        if namespace is not None and namespace not in _CLEANUP_NAMESPACES:
            raise _failure("protocol-incompatible", "Cleanup namespace is unsupported.")
        raw_run_ids = value.get("run_ids", [])
        if type(raw_run_ids) is not list:
            raise _failure("protocol-incompatible", "run_ids must be a JSON array.")
        run_ids = tuple(_canonical_run_id(item) for item in raw_run_ids)
        if len(set(run_ids)) != len(run_ids):
            raise _failure("protocol-incompatible", "run_ids must be unique.")
        older_than_seconds = value.get("older_than_seconds", 86_400)
        if (
            type(older_than_seconds) is not int
            or not 86_400 <= older_than_seconds <= 31_536_000
        ):
            raise _failure(
                "protocol-incompatible",
                "older_than_seconds must be an integer between one day and one year.",
            )
        records = self._retained_run_records()
        record_by_id = {record["run_id"]: record for record in records}
        missing = sorted(set(run_ids) - set(record_by_id))
        if missing:
            raise _failure(
                "cleanup-conflict",
                "A selected terminal run is not retained.",
                phase="cleanup-planning",
                next_action="refresh-cleanup-selection",
            )
        referenced_deployments = {record["deployment_id"] for record in records}
        referenced_objects = {record["object_id"] for record in records}
        candidates: list[dict[str, Any]] = []

        def add_candidate(
            candidate_namespace: str,
            relative_path: str,
            *,
            reference_reasons: tuple[str, ...] = (),
            consequences: tuple[str, ...] = (),
        ) -> None:
            identity, size = _cleanup_tree_snapshot(self.root, relative_path)
            candidates.append(
                self._cleanup_candidate(
                    candidate_namespace,
                    identity,
                    relative_path,
                    size,
                    reference_reasons=reference_reasons,
                    consequences=consequences,
                )
            )

        if namespace in {None, "temporary"}:
            for relative_path in self._temporary_cleanup_paths(older_than_seconds):
                add_candidate(
                    "temporary",
                    relative_path,
                    consequences=("interrupted unpublished material will be unavailable",),
                )
        if namespace in {None, "deployments"}:
            for child in sorted((self.root / "deployments").iterdir(), key=lambda item: item.name):
                deployment_id = f"sha256:{child.name}"
                if child.name.startswith(".cleanup-") or deployment_id in referenced_deployments:
                    continue
                if DIGEST_RE.fullmatch(deployment_id) is None:
                    raise _failure("cleanup-conflict", "The deployment inventory is malformed.")
                add_candidate(
                    "deployments",
                    child.relative_to(self.root).as_posix(),
                    consequences=("deployment must be published again before reuse",),
                )
        if namespace in {None, "objects"}:
            for child in sorted((self.root / "objects").iterdir(), key=lambda item: item.name):
                if child.name.startswith(".cleanup-"):
                    continue
                if not child.name.endswith(".object"):
                    raise _failure("cleanup-conflict", "The object inventory is malformed.")
                object_id = f"sha256:{child.name[:-7]}"
                if DIGEST_RE.fullmatch(object_id) is None:
                    raise _failure("cleanup-conflict", "The object inventory is malformed.")
                if object_id not in referenced_objects:
                    add_candidate(
                        "objects",
                        child.relative_to(self.root).as_posix(),
                        consequences=("uploaded object must be transferred again before reuse",),
                    )
        if namespace in {None, "runs"}:
            for run_id in sorted(run_ids):
                record = record_by_id[run_id]
                if not self._run_is_terminal(record):
                    raise _failure(
                        "cleanup-conflict",
                        "Cleanup refuses a selected non-terminal run.",
                        phase="cleanup-planning",
                        next_action="wait-for-terminal-run",
                        identities={"run_id": run_id},
                    )
                add_candidate(
                    "runs",
                    f"runs/{run_id}",
                    reference_reasons=("explicitly selected terminal run",),
                    consequences=(
                        "attachment, diagnostics, and retry history will be unavailable",
                        "results owned only by this run record may become unavailable",
                    ),
                )
        candidates.sort(key=lambda item: (item["namespace"], item["path"]))
        root_revision = self._cleanup_reference_revision(records)
        payload = {
            "schema": _CLEANUP_PLAN_SCHEMA,
            "root_revision": root_revision,
            "candidates": candidates,
        }
        return {**payload, "plan_id": self._cleanup_plan_id(payload)}

    def apply_cleanup(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        value = _exact_arguments(arguments, {"plan"})
        plan = value["plan"]
        if type(plan) is not dict or set(plan) != {
            "schema", "plan_id", "root_revision", "candidates"
        } or plan.get("schema") != _CLEANUP_PLAN_SCHEMA:
            raise _failure("protocol-incompatible", "The cleanup plan is malformed.")
        payload = {
            "schema": plan["schema"],
            "root_revision": plan["root_revision"],
            "candidates": plan["candidates"],
        }
        if (
            type(plan["plan_id"]) is not str
            or not hmac.compare_digest(plan["plan_id"], self._cleanup_plan_id(payload))
            or type(plan["root_revision"]) is not int
            or type(plan["candidates"]) is not list
        ):
            raise _failure("cleanup-conflict", "The cleanup plan identity is invalid.")
        records = self._retained_run_records()
        current_revision = self._cleanup_reference_revision(records)
        removed: list[str] = []
        skipped: dict[str, str] = {}
        reference_revision_changed = current_revision != plan["root_revision"]
        referenced_deployments = {record["deployment_id"] for record in records}
        referenced_objects = {record["object_id"] for record in records}
        expected_fields = {
            "namespace", "identity", "path", "size", "reference_reasons", "consequences"
        }
        seen_paths: set[str] = set()
        for candidate in plan["candidates"]:
            if (
                type(candidate) is not dict
                or set(candidate) != expected_fields
                or candidate["namespace"] not in _CLEANUP_NAMESPACES
                or type(candidate["identity"]) is not str
                or DIGEST_RE.fullmatch(candidate["identity"]) is None
                or type(candidate["path"]) is not str
                or type(candidate["size"]) is not int
                or type(candidate["reference_reasons"]) is not list
                or type(candidate["consequences"]) is not list
                or candidate["path"] in seen_paths
            ):
                raise _failure("protocol-incompatible", "A cleanup candidate is malformed.")
            seen_paths.add(candidate["path"])
            logical = PurePosixPath(candidate["path"])
            identity = candidate["identity"]
            if not logical.parts or logical.parts[0] != candidate["namespace"]:
                skipped[identity] = "cleanup-conflict:namespace-mismatch"
                continue
            tombstone_name = (
                f".cleanup-{plan['plan_id'][7:23]}-{identity[7:23]}.deleting"
            )
            resuming = False
            if reference_revision_changed:
                check_descriptors: list[int] = []
                try:
                    namespace_descriptor, parent_descriptor, check_descriptors = (
                        _open_cleanup_parent(self.root, logical)
                    )
                    try:
                        os.stat(
                            logical.name,
                            dir_fd=parent_descriptor,
                            follow_symlinks=False,
                        )
                    except FileNotFoundError:
                        try:
                            os.stat(
                                tombstone_name,
                                dir_fd=namespace_descriptor,
                                follow_symlinks=False,
                            )
                        except FileNotFoundError:
                            removed.append(identity)
                            continue
                        resuming = True
                    else:
                        skipped[identity] = (
                            "cleanup-conflict:reference-revision-changed"
                        )
                        continue
                except (FileNotFoundError, GatewayOperationFailure):
                    skipped[identity] = "cleanup-conflict:reference-revision-changed"
                    continue
                finally:
                    for descriptor in reversed(check_descriptors):
                        os.close(descriptor)
            if not resuming and candidate["namespace"] == "deployments":
                deployment_id = f"sha256:{logical.name}"
                if deployment_id in referenced_deployments:
                    skipped[identity] = "cleanup-conflict:retained-run-reference"
                    continue
            elif not resuming and candidate["namespace"] == "objects":
                object_id = f"sha256:{logical.name.removesuffix('.object')}"
                if object_id in referenced_objects:
                    skipped[identity] = "cleanup-conflict:retained-run-reference"
                    continue
            elif not resuming and candidate["namespace"] == "runs":
                record = next(
                    (item for item in records if item["run_id"] == logical.name), None
                )
                if record is None or not self._run_is_terminal(record):
                    skipped[identity] = "cleanup-conflict:run-not-terminal"
                    continue
            elif not resuming and candidate["namespace"] == "transfers":
                skipped[identity] = "cleanup-conflict:transfer-lease-unverified"
                continue
            namespace_root = self.root / candidate["namespace"]
            tombstone = namespace_root / tombstone_name
            descriptors: list[int] = []
            try:
                namespace_descriptor, parent_descriptor, descriptors = (
                    _open_cleanup_parent(self.root, logical)
                )
                try:
                    source_metadata = os.stat(
                        logical.name,
                        dir_fd=parent_descriptor,
                        follow_symlinks=False,
                    )
                except FileNotFoundError:
                    source_metadata = None
                if source_metadata is not None:
                    observed_identity, observed_size = _cleanup_tree_snapshot(
                        self.root, candidate["path"]
                    )
                    if observed_identity != identity or observed_size != candidate["size"]:
                        skipped[identity] = "cleanup-conflict:candidate-changed"
                        continue
                    try:
                        os.stat(
                            tombstone_name,

                            dir_fd=namespace_descriptor,
                            follow_symlinks=False,
                        )
                    except FileNotFoundError:
                        pass
                    else:
                        skipped[identity] = "cleanup-conflict:tombstone-exists"
                        continue
                    os.rename(
                        logical.name,
                        tombstone_name,
                        src_dir_fd=parent_descriptor,
                        dst_dir_fd=namespace_descriptor,
                    )
                    os.fsync(namespace_descriptor)
                    tombstone_metadata = os.stat(
                        tombstone_name,
                        dir_fd=namespace_descriptor,
                        follow_symlinks=False,
                    )
                    before = (
                        source_metadata.st_dev,
                        source_metadata.st_ino,
                        source_metadata.st_mode,
                        source_metadata.st_uid,
                        source_metadata.st_nlink,
                        source_metadata.st_size,
                    )
                    after = (
                        tombstone_metadata.st_dev,
                        tombstone_metadata.st_ino,
                        tombstone_metadata.st_mode,
                        tombstone_metadata.st_uid,
                        tombstone_metadata.st_nlink,
                        tombstone_metadata.st_size,
                    )
                    if before != after:
                        try:
                            os.rename(
                                tombstone_name,
                                logical.name,
                                src_dir_fd=namespace_descriptor,
                                dst_dir_fd=parent_descriptor,
                            )
                            os.fsync(namespace_descriptor)
                            if parent_descriptor != namespace_descriptor:
                                os.fsync(parent_descriptor)
                        except FileExistsError:
                            pass
                        skipped[identity] = "cleanup-conflict:candidate-replaced"
                        continue
                    observed_identity, observed_size = _cleanup_tree_snapshot(
                        self.root, candidate["path"], physical_path=tombstone
                    )
                    if observed_identity != identity or observed_size != candidate["size"]:
                        try:
                            os.rename(
                                tombstone_name,
                                logical.name,
                                src_dir_fd=namespace_descriptor,
                                dst_dir_fd=parent_descriptor,
                            )
                            os.fsync(namespace_descriptor)
                            if parent_descriptor != namespace_descriptor:
                                os.fsync(parent_descriptor)
                        except FileExistsError:
                            pass
                        skipped[identity] = "cleanup-conflict:candidate-changed"
                        continue
                else:
                    try:
                        os.stat(
                            tombstone_name,
                            dir_fd=namespace_descriptor,
                            follow_symlinks=False,
                        )
                    except FileNotFoundError:
                        removed.append(identity)
                        continue
                    observed_identity, observed_size = _cleanup_tree_snapshot(
                        self.root, candidate["path"], physical_path=tombstone
                    )
                    if observed_identity != identity or observed_size != candidate["size"]:
                        skipped[identity] = "cleanup-conflict:tombstone-changed"
                        continue
                _unlink_tree_at(namespace_descriptor, tombstone_name)
                os.fsync(namespace_descriptor)
                removed.append(identity)
            except (FileNotFoundError, GatewayOperationFailure):
                skipped[identity] = "cleanup-conflict:candidate-changed"
            finally:
                for descriptor in reversed(descriptors):
                    os.close(descriptor)
        return {
            "schema": _CLEANUP_REPORT_SCHEMA,
            "plan_id": plan["plan_id"],
            "removed": removed,
            "skipped": skipped,
        }



__all__ = ["GatewayCleanupMixin"]
