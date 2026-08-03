"""Revision-bound retry and recomputation of retained submitted runs."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import shutil
import stat
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Generic, TypeVar

from bioimageflow.storage import canonical_json_bytes

from .configuration import import_config_factory, verify_secret_references
from .errors import PSIJSubmissionUncertainError, WorkflowRunRetryError
from .payload import load_workflow_payload
from .repository import (
    LauncherCorruptionError,
    LauncherRepository,
    RunNotFoundError,
    _atomic_write_json,
    _read_json,
)
from .schemas import RETRY_SUBMISSION_SCHEMA, TERMINAL_STATES, utc_timestamp, validate_run_id
from .submission import _launch_prepared_control
from .types import ParslConfigRef, launch_config_from_dict

if TYPE_CHECKING:
    from .run import WorkflowRun

_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_RetryRun = TypeVar("_RetryRun")
_RETRY_TRANSACTION_SCHEMA = "bioimageflow.launcher.retry-transaction.v1"

@dataclass(frozen=True, slots=True)
class RecomputeRequest:
    """Scoped cache invalidation requested before a retained run is retried."""

    SCHEMA: ClassVar[str] = "bioimageflow.recompute_request.v1"
    node_paths: tuple[str, ...]
    cascade: bool = True

    def __post_init__(self) -> None:
        if type(self.node_paths) is not tuple or not self.node_paths:
            raise ValueError("node_paths must be a non-empty tuple.")
        if any(
            type(path) is not str
            or not path
            or path != path.strip()
            or any(part in {"", ".", ".."} for part in path.split("/"))
            for path in self.node_paths
        ):
            raise ValueError("node_paths must contain safe scoped node paths.")
        if len(set(self.node_paths)) != len(self.node_paths):
            raise ValueError("node_paths must be unique.")
        if type(self.cascade) is not bool:
            raise TypeError("cascade must be a bool.")
        object.__setattr__(self, "node_paths", tuple(sorted(self.node_paths)))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "node_paths": list(self.node_paths),
            "cascade": self.cascade,
        }
    @classmethod
    def from_dict(cls, value: Any) -> "RecomputeRequest":
        if (
            type(value) is not dict
            or set(value) != {"schema", "node_paths", "cascade"}
            or value["schema"] != cls.SCHEMA
            or type(value["node_paths"]) is not list
        ):
            raise ValueError("Invalid RecomputeRequest payload.")
        return cls(tuple(value["node_paths"]), value["cascade"])

@dataclass(frozen=True, slots=True)
class RetryInvalidation:
    """One current cache selection selected by a recomputation preview."""

    node_path: str
    result_key: str
    record_id: str | None
    selection_status: str

    def __post_init__(self) -> None:
        if (
            type(self.node_path) is not str
            or not self.node_path
            or self.node_path != self.node_path.strip()
            or any(part in {"", ".", ".."} for part in self.node_path.split("/"))
        ):
            raise ValueError("Retry invalidation node_path is invalid.")
        if type(self.result_key) is not str or not self.result_key:
            raise ValueError("Retry invalidation result_key is invalid.")
        if self.record_id is not None and (
            type(self.record_id) is not str or not self.record_id
        ):
            raise ValueError("Retry invalidation record_id is invalid.")
        if (
            type(self.selection_status) is not str
            or self.selection_status not in {"selected", "corrupt"}
        ):
            raise ValueError("Retry invalidation status is invalid.")

    def to_dict(self) -> dict[str, Any]:
        return {
            "node_path": self.node_path,
            "result_key": self.result_key,
            "record_id": self.record_id,
            "selection_status": self.selection_status,
        }
    @classmethod
    def from_dict(cls, value: Any) -> "RetryInvalidation":
        if type(value) is not dict or set(value) != {
            "node_path",
            "result_key",
            "record_id",
            "selection_status",
        }:
            raise ValueError("Invalid RetryInvalidation payload.")
        return cls(
            value["node_path"],
            value["result_key"],
            value["record_id"],
            value["selection_status"],
        )

@dataclass(frozen=True, slots=True)
class RunRetryPlan:
    """Immutable retry preview binding parent state and invalidation intent."""

    SCHEMA: ClassVar[str] = "bioimageflow.run_retry_plan.v1"
    parent_run_id: str
    retry_run_id: str
    parent_status: str
    parent_status_revision: int
    storage_path: str
    retained_submission_digest: str
    retained_material_digest: str
    retained_material_entries: int
    cache_selection_revision: str
    recompute: RecomputeRequest | None
    invalidations: tuple[RetryInvalidation, ...]
    conflicting_run_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        validate_run_id(self.parent_run_id)
        validate_run_id(self.retry_run_id)
        if self.parent_run_id == self.retry_run_id:
            raise ValueError("Retry run ID must differ from its parent.")
        if self.parent_status not in TERMINAL_STATES:
            raise ValueError("Retry parent status must be terminal.")
        if type(self.parent_status_revision) is not int or self.parent_status_revision < 0:
            raise ValueError("Parent status revision is invalid.")
        if (
            type(self.storage_path) is not str
            or not Path(self.storage_path).is_absolute()
            or Path(self.storage_path).as_posix() != self.storage_path
        ):
            raise ValueError("Retry storage path is invalid.")
        for digest in (
            self.retained_submission_digest,
            self.retained_material_digest,
        ):
            if type(digest) is not str or _SHA256.fullmatch(digest) is None:
                raise ValueError("Retained retry material digest is invalid.")
        if (
            type(self.retained_material_entries) is not int
            or self.retained_material_entries < 0
        ):
            raise ValueError("Retained retry material entry count is invalid.")
        if (
            type(self.cache_selection_revision) is not str
            or _SHA256.fullmatch(self.cache_selection_revision) is None
        ):
            raise ValueError("Cache revision is invalid.")
        if self.recompute is not None and type(self.recompute) is not RecomputeRequest:
            raise TypeError("recompute must be a RecomputeRequest or None.")
        if type(self.invalidations) is not tuple or any(
            type(item) is not RetryInvalidation for item in self.invalidations
        ):
            raise TypeError("invalidations must contain RetryInvalidation values.")
        invalidation_keys = tuple(
            (item.node_path, item.result_key) for item in self.invalidations
        )
        if invalidation_keys != tuple(sorted(set(invalidation_keys))):
            raise ValueError("invalidations must be unique and canonically ordered.")
        if self.recompute is None and self.invalidations:
            raise ValueError("invalidations require a recompute request.")
        if type(self.conflicting_run_ids) is not tuple:
            raise TypeError("conflicting_run_ids must be a tuple.")
        for run_id in self.conflicting_run_ids:
            validate_run_id(run_id)
        if self.conflicting_run_ids != tuple(sorted(set(self.conflicting_run_ids))):
            raise ValueError("conflicting_run_ids must be unique and canonically ordered.")

    def _body(self) -> dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "parent_run_id": self.parent_run_id,
            "retry_run_id": self.retry_run_id,
            "parent_status": self.parent_status,
            "parent_status_revision": self.parent_status_revision,
            "storage_path": self.storage_path,
            "retained_submission_digest": self.retained_submission_digest,
            "retained_material_digest": self.retained_material_digest,
            "retained_material_entries": self.retained_material_entries,
            "cache_selection_revision": self.cache_selection_revision,
            "recompute": None if self.recompute is None else self.recompute.to_dict(),
            "invalidations": [item.to_dict() for item in self.invalidations],
            "conflicting_run_ids": list(self.conflicting_run_ids),
        }

    @property
    def digest(self) -> str:
        return f"sha256:{hashlib.sha256(canonical_json_bytes(self._body())).hexdigest()}"

    def to_dict(self) -> dict[str, Any]:
        return {**self._body(), "digest": self.digest}

    @classmethod
    def from_dict(cls, value: Any) -> "RunRetryPlan":
        fields = {
            "schema",
            "digest",
            "parent_run_id",
            "retry_run_id",
            "parent_status",
            "parent_status_revision",
            "storage_path",
            "retained_submission_digest",
            "retained_material_digest",
            "retained_material_entries",
            "cache_selection_revision",
            "recompute",
            "invalidations",
            "conflicting_run_ids",
        }
        if (
            type(value) is not dict
            or set(value) != fields
            or value["schema"] != cls.SCHEMA
            or type(value["invalidations"]) is not list
            or type(value["conflicting_run_ids"]) is not list
        ):
            raise ValueError("Invalid RunRetryPlan payload.")
        plan = cls(
            parent_run_id=value["parent_run_id"],
            retry_run_id=value["retry_run_id"],
            parent_status=value["parent_status"],
            parent_status_revision=value["parent_status_revision"],
            storage_path=value["storage_path"],
            retained_submission_digest=value["retained_submission_digest"],
            retained_material_digest=value["retained_material_digest"],
            retained_material_entries=value["retained_material_entries"],
            cache_selection_revision=value["cache_selection_revision"],
            recompute=(
                None
                if value["recompute"] is None
                else RecomputeRequest.from_dict(value["recompute"])
            ),
            invalidations=tuple(
                RetryInvalidation.from_dict(item) for item in value["invalidations"]
            ),
            conflicting_run_ids=tuple(value["conflicting_run_ids"]),
        )
        if value["digest"] != plan.digest:
            raise ValueError("RunRetryPlan digest mismatch.")
        return plan


class PreparedRunRetry(Generic[_RetryRun]):
    """Single-use revision-bound retry prepared from one retained run."""

    def __init__(
        self,
        plan: RunRetryPlan,
        submitter: Callable[[], _RetryRun],
    ) -> None:
        if type(plan) is not RunRetryPlan or not callable(submitter):
            raise TypeError("PreparedRunRetry requires a plan and submit callback.")
        self.plan = plan
        self._submitter = submitter
        self._submitted = False

    @property
    def submitted(self) -> bool:
        return self._submitted

    @property
    def can_submit(self) -> bool:
        """Whether this object is unused and its preview found no active run."""
        return not self._submitted and not self.plan.conflicting_run_ids

    def submit(self) -> _RetryRun:
        """Apply the bound intent and create exactly one new submitted run."""
        if self._submitted:
            raise RuntimeError("Prepared retry was already submitted.")
        self._submitted = True
        return self._submitter()


def _cache_selection_revision(storage_path: Path) -> str:
    digest = hashlib.sha256()
    root = storage_path / "cache" / "v1" / "results"
    if root.exists():
        if root.is_symlink() or not root.is_dir():
            raise WorkflowRunRetryError("Cache results root is unsafe.")
        for path in sorted(root.rglob("current.json"), key=lambda item: item.as_posix()):
            if path.is_symlink() or not path.is_file():
                raise WorkflowRunRetryError("Cache selection is unsafe.")
            relative = path.relative_to(root).as_posix().encode("utf-8")
            encoded = path.read_bytes()
            digest.update(len(relative).to_bytes(8, "big"))
            digest.update(relative)
            digest.update(len(encoded).to_bytes(8, "big"))
            digest.update(encoded)
    return f"sha256:{digest.hexdigest()}"


def _submission_digest(submission: dict[str, Any]) -> str:
    return f"sha256:{hashlib.sha256(canonical_json_bytes(submission)).hexdigest()}"


def _retained_material_identity(root: Path) -> tuple[str, int]:
    entries: list[dict[str, Any]] = []
    for directory_name in ("inputs", "bootstrap"):
        directory = root / directory_name
        if not directory.exists():
            continue
        if directory.is_symlink() or not directory.is_dir():
            raise WorkflowRunRetryError("Retained invocation assets are unsafe.")
        entries.append({"kind": "directory", "path": directory_name})
        for path in sorted(directory.rglob("*"), key=lambda item: item.as_posix()):
            relative = path.relative_to(root).as_posix()
            mode = path.lstat().st_mode
            if stat.S_ISLNK(mode):
                raise WorkflowRunRetryError(
                    "Retained invocation assets contain a symlink."
                )
            if stat.S_ISDIR(mode):
                entries.append({"kind": "directory", "path": relative})
                continue
            if not stat.S_ISREG(mode):
                raise WorkflowRunRetryError(
                    "Retained invocation assets contain a special file."
                )
            before = path.stat()
            encoded = path.read_bytes()
            after = path.stat()
            if (
                before.st_ino != after.st_ino
                or before.st_size != after.st_size
                or before.st_mtime_ns != after.st_mtime_ns
            ):
                raise WorkflowRunRetryError(
                    "Retained invocation asset changed while inspected."
                )
            entries.append(
                {
                    "digest": f"sha256:{hashlib.sha256(encoded).hexdigest()}",
                    "kind": "file",
                    "path": relative,
                    "size": len(encoded),
                }
            )
    digest = hashlib.sha256(canonical_json_bytes({"entries": entries})).hexdigest()
    return f"sha256:{digest}", len(entries)


def _active_run_ids(repository: LauncherRepository) -> tuple[str, ...]:
    active: set[str] = set()
    if repository.runs_root.exists():
        for path in repository.runs_root.iterdir():
            if path.name.startswith(".") or not path.is_dir() or path.is_symlink():
                continue
            run = repository.open(path.name)
            if run.read_status()["state"] not in TERMINAL_STATES:
                active.add(path.name)
    if repository.canonical_runs_root.exists():
        for path in repository.canonical_runs_root.iterdir():
            metadata = path / "run.json"
            if path.is_symlink() or not path.is_dir() or not metadata.is_file():
                continue
            try:
                value = json.loads(metadata.read_text())
            except (OSError, json.JSONDecodeError) as exc:
                raise WorkflowRunRetryError("Canonical run metadata is invalid.") from exc
            if type(value) is dict and value.get("status") == "running":
                active.add(path.name)
    return tuple(sorted(active))


def _invalidation_preview(workflow: Any, request: RecomputeRequest | None) -> tuple[RetryInvalidation, ...]:
    if request is None:
        return ()
    selections = workflow._preview_invalidation(
        request.node_paths,
        cascade=request.cascade,
    )
    return tuple(
        sorted(
            (
                RetryInvalidation(
                    node_path=item.node_name,
                    result_key=item.result_key,
                    record_id=item.selected_record_id,
                    selection_status=(
                        "corrupt" if item.status == "corrupt_removed" else "selected"
                    ),
                )
                for item in selections
            ),
            key=lambda item: (item.node_path, item.result_key),
        )
    )


def prepare_retry_plan(run: Any, recompute: RecomputeRequest | None) -> RunRetryPlan:
    """Build one non-mutating retry plan from a retained terminal run."""
    if recompute is not None and type(recompute) is not RecomputeRequest:
        raise TypeError("recompute must be a RecomputeRequest or None.")
    run.refresh()
    status = run._control.read_status()
    if status["state"] not in TERMINAL_STATES:
        raise WorkflowRunRetryError(
            "Only terminal submitted runs can be retried.",
            details={"run_id": run.id, "state": status["state"]},
        )
    submission = run._control.read_submission()
    storage = Path(submission["storage_root"])
    repository = LauncherRepository(storage)
    workflow = load_workflow_payload(submission["workflow"], storage_path=storage)
    with repository.allocation_guard():
        current = run._control.read_status()
        if current["state"] != status["state"] or current["revision"] != status["revision"]:
            raise WorkflowRunRetryError("Parent run changed while retry was prepared.")
        material_digest, material_entries = _retained_material_identity(
            run.control_dir
        )
        return RunRetryPlan(
            parent_run_id=run.id,
            retry_run_id=repository.new_run_id(),
            parent_status=status["state"],
            parent_status_revision=status["revision"],
            storage_path=storage.as_posix(),
            retained_submission_digest=_submission_digest(submission),
            retained_material_digest=material_digest,
            retained_material_entries=material_entries,
            cache_selection_revision=_cache_selection_revision(storage),
            recompute=recompute,
            invalidations=_invalidation_preview(workflow, recompute),
            conflicting_run_ids=tuple(
                value for value in _active_run_ids(repository) if value != run.id
            ),
        )


def _copy_retained_tree(source: Path, destination: Path) -> None:
    if not source.exists():
        return
    if source.is_symlink() or not source.is_dir():
        raise WorkflowRunRetryError("Retained invocation assets are unsafe.")
    destination.mkdir()
    for child in sorted(source.iterdir(), key=lambda item: item.name):
        target = destination / child.name
        mode = child.lstat().st_mode
        if stat.S_ISLNK(mode):
            raise WorkflowRunRetryError("Retained invocation assets contain a symlink.")
        if stat.S_ISDIR(mode):
            _copy_retained_tree(child, target)
        elif stat.S_ISREG(mode):
            before = child.stat()
            shutil.copyfile(child, target)
            after = child.stat()
            if (
                before.st_ino != after.st_ino
                or before.st_size != after.st_size
                or before.st_mtime_ns != after.st_mtime_ns
            ):
                raise WorkflowRunRetryError("Retained invocation asset changed while copied.")
        else:
            raise WorkflowRunRetryError("Retained invocation assets contain a special file.")


def _clone_submission(run: Any, plan: RunRetryPlan, candidate: Path) -> dict[str, Any]:
    source = run._control.read_submission()
    _copy_retained_tree(run.control_dir / "inputs", candidate / "inputs")
    _copy_retained_tree(run.control_dir / "bootstrap", candidate / "bootstrap")
    result = copy.deepcopy(source)
    result.update(
        {
            "schema": RETRY_SUBMISSION_SCHEMA,
            "run_id": plan.retry_run_id,
            "parent_run_id": plan.parent_run_id,
            "retry_plan": plan.to_dict(),
            "created_at": utc_timestamp(),
            "canonical_view": f"views/runs/{plan.retry_run_id}",
        }
    )
    return result


def _retry_transaction_path(control: Any) -> Path:
    return control.confined_path("retry_transaction.json")


def _write_retry_transaction(control: Any, plan: RunRetryPlan, phase: str) -> None:
    if phase not in {"validated", "invalidated", "dispatched", "uncertain"}:
        raise AssertionError(f"Unknown retry transaction phase: {phase!r}.")
    _atomic_write_json(
        _retry_transaction_path(control),
        {
            "schema": _RETRY_TRANSACTION_SCHEMA,
            "run_id": plan.retry_run_id,
            "plan_digest": plan.digest,
            "phase": phase,
        },
    )


def _read_retry_transaction(control: Any, plan: RunRetryPlan) -> str | None:
    path = _retry_transaction_path(control)
    try:
        value = _read_json(path)
    except FileNotFoundError:
        return None
    if (
        set(value) != {"schema", "run_id", "plan_digest", "phase"}
        or value["schema"] != _RETRY_TRANSACTION_SCHEMA
        or value["run_id"] != plan.retry_run_id
        or value["plan_digest"] != plan.digest
        or value["phase"]
        not in {"validated", "invalidated", "dispatched", "uncertain"}
    ):
        raise WorkflowRunRetryError("Retained retry transaction is invalid.")
    return value["phase"]

def _invalidation_paths(
    storage: Path,
    plan: RunRetryPlan,
) -> list[tuple[RetryInvalidation, Path, Path]]:
    from bioimageflow.storage import Storage

    repository = Storage(storage)
    return [
        (
            item,
            repository.result_dir(item.result_key) / "current.json",
            repository.result_dir(item.result_key)
            / f".current.{plan.retry_run_id}.retry-backup",
        )
        for item in plan.invalidations
    ]


def _pointer_matches(item: RetryInvalidation, path: Path) -> bool:
    try:
        value = _read_json(path)
    except (LauncherCorruptionError, OSError, ValueError):
        return item.selection_status == "corrupt" and item.record_id is None
    return value.get("record_id") == item.record_id


def _finish_invalidations(storage: Path, plan: RunRetryPlan) -> None:
    """Finish a journaled invalidation after interruption at any pointer."""
    for item, current, backup in _invalidation_paths(storage, plan):
        current_exists = current.exists() or current.is_symlink()
        backup_exists = backup.exists() or backup.is_symlink()
        if current_exists and backup_exists:
            raise WorkflowRunRetryError(
                "A retry invalidation has conflicting current and backup pointers."
            )
        if not current_exists and not backup_exists:
            raise WorkflowRunRetryError(
                "A retry invalidation pointer disappeared before it was retained."
            )
        source = backup if backup_exists else current
        mode = source.lstat().st_mode
        if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
            raise WorkflowRunRetryError("A selected cache pointer is unsafe.")
        if not _pointer_matches(item, source):
            raise WorkflowRunRetryError(
                "A selected cache pointer changed during retry invalidation."
            )
        if current_exists:
            os.replace(current, backup)


def _cleanup_invalidation_backups(storage: Path, plan: RunRetryPlan) -> None:
    """Remove rollback copies after the durable invalidated phase, best effort."""
    for _item, current, backup in _invalidation_paths(storage, plan):
        if current.exists() or current.is_symlink():
            raise WorkflowRunRetryError(
                "An invalidated cache pointer was unexpectedly restored."
            )
        try:
            backup.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            # The durable phase already makes the absent current pointer final.
            # A retained hidden backup is safe and a later replay cleans it up.
            pass


def _retry_launch_state(control: Any, backend: str) -> str:
    if backend == "local":
        if control.confined_path("local_process.json").exists():
            return "dispatched"
        if any(
            control.confined_path(relative).exists()
            for relative in ("logs/orchestrator.out", "logs/orchestrator.err")
        ):
            return "uncertain"
        return "not-started"
    if backend == "manual":
        return (
            "dispatched"
            if control.confined_path("command.json").exists()
            else "not-started"
        )
    if backend == "psij":
        return (
            "reconnect"
            if control.confined_path("psij_intent.json").exists()
            else "not-started"
        )
    raise AssertionError(f"Unknown retry launcher backend: {backend!r}.")


def _launch_or_reconnect_retry(
    control: Any,
    launch: Any,
    config_ref: ParslConfigRef,
) -> None:
    """Launch only before any durable backend attempt marker exists."""
    status = control.read_status()
    if status["state"] != "prepared":
        return
    launch_state = _retry_launch_state(control, launch.backend)
    if launch_state == "dispatched":
        return
    if launch_state == "uncertain":
        raise WorkflowRunRetryError(
            "The retained retry launcher outcome is uncertain; reconnect to the "
            "child run without resubmitting it.",
            details={
                "code": "retry-submission-uncertain",
                "retry_run_id": control.run_id,
            },
        )
    if launch_state == "reconnect":
        # PSI/J's immutable intent makes this attach-or-uncertain path safe.
        _launch_prepared_control(control, launch, parsl_config=config_ref)
        return
    _launch_prepared_control(control, launch, parsl_config=config_ref)


def submit_retry_plan(run: Any, plan: RunRetryPlan) -> "WorkflowRun":
    """Validate and apply one immutable retry plan, then launch its new run."""
    if type(plan) is not RunRetryPlan or plan.parent_run_id != run.id:
        raise WorkflowRunRetryError("Retry plan does not belong to this run.")
    submission = run._control.read_submission()
    storage = Path(submission["storage_root"])
    if storage.as_posix() != plan.storage_path:
        raise WorkflowRunRetryError("Retry plan storage binding does not match this run.")
    repository = LauncherRepository(storage)
    control = None
    try:
        existing = repository.open(plan.retry_run_id)
    except RunNotFoundError:
        pass
    else:
        retained = existing.read_submission()
        if (
            retained.get("parent_run_id") != plan.parent_run_id
            or retained.get("retry_plan") != plan.to_dict()
        ):
            raise WorkflowRunRetryError("Retry run ID is already owned by another run.")
        control = existing

    config_ref = ParslConfigRef.from_dict(submission["parsl_config"])
    import_config_factory(config_ref.factory)
    verify_secret_references(config_ref)
    workflow = load_workflow_payload(submission["workflow"], storage_path=storage)
    launch = launch_config_from_dict(submission["launch"])
    candidate: Path | None = None
    try:
        if control is None:
            candidate = repository.create_candidate(plan.retry_run_id)
            cloned = _clone_submission(run, plan, candidate)
        with repository.allocation_guard():
            retained_digest, retained_entries = _retained_material_identity(
                run.control_dir
            )
            if (
                _submission_digest(run._control.read_submission())
                != plan.retained_submission_digest
                or retained_digest != plan.retained_material_digest
                or retained_entries != plan.retained_material_entries
            ):
                raise WorkflowRunRetryError(
                    "Retained submission material changed after retry preparation."
                )
            if candidate is not None:
                candidate_digest, candidate_entries = _retained_material_identity(
                    candidate
                )
                if (
                    candidate_digest != retained_digest
                    or candidate_entries != retained_entries
                ):
                    raise WorkflowRunRetryError(
                        "Retained submission material changed while cloned."
                    )
            status = run._control.read_status()
            if (
                status["state"] != plan.parent_status
                or status["revision"] != plan.parent_status_revision
            ):
                raise WorkflowRunRetryError(
                    "Parent run revision no longer matches the prepared retry.",
                    details={"expected_revision": plan.parent_status_revision},
                )
            conflicts = tuple(
                value
                for value in _active_run_ids(repository)
                if value not in {run.id, plan.retry_run_id}
            )
            if conflicts:
                raise WorkflowRunRetryError(
                    "Retry conflicts with an active execution.",
                    details={"conflicting_run_ids": list(conflicts)},
                )
            phase = (
                None if control is None else _read_retry_transaction(control, plan)
            )
            if phase is None and plan.recompute is not None:
                revision = _cache_selection_revision(storage)
                preview = _invalidation_preview(workflow, plan.recompute)
                if (
                    revision != plan.cache_selection_revision
                    or preview != plan.invalidations
                ):
                    raise WorkflowRunRetryError(
                        "Cache selections changed after retry preparation.",
                        details={
                            "expected_cache_selection_revision": (
                                plan.cache_selection_revision
                            ),
                            "actual_cache_selection_revision": revision,
                        },
                    )
            if control is None:
                assert candidate is not None
                control = repository.allocate(
                    cloned,
                    backend=launch.backend,
                    candidate_dir=candidate,
                    allocation_guard_held=True,
                )
                candidate = None
            if phase is None:
                _write_retry_transaction(control, plan, "validated")
                phase = "validated"
            if phase == "validated":
                _finish_invalidations(storage, plan)
                _write_retry_transaction(control, plan, "invalidated")
            _cleanup_invalidation_backups(storage, plan)
    finally:
        if candidate is not None and candidate.exists() and not candidate.is_symlink():
            shutil.rmtree(candidate)
    assert control is not None
    if phase not in {"dispatched", "uncertain"}:
        try:
            _launch_or_reconnect_retry(control, launch, config_ref)
        except PSIJSubmissionUncertainError:
            with repository.allocation_guard():
                _write_retry_transaction(control, plan, "uncertain")
            raise
        with repository.allocation_guard():
            _write_retry_transaction(control, plan, "dispatched")
    from .run import WorkflowRun

    return WorkflowRun(control)


__all__ = [
    "PreparedRunRetry",
    "RecomputeRequest",
    "RetryInvalidation",
    "RunRetryPlan",
]
