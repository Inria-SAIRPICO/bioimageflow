"""Guarded per-run status, progress, cancellation, and claim operations."""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .repository import (
    ClaimConflictError,
    ClaimExpiredError,
    ClaimResult,
    LauncherCorruptionError,
    LauncherStorageError,
    _assert_confined_path,
    _assert_directory,
    _atomic_create_json,
    _atomic_write_json,
    _canonical_json_bytes,
    _CrossProcessLock,
    _decode_json,
    _is_symlink,
    _normalized_now,
    _path_exists,
    _read_bytes,
    _read_json,
    _require_positive_lease,
    _sync_directory,
    _validate_relative_path,
)
from .schemas import (
    CLAIM_SCHEMA,
    PROGRESS_SCHEMA,
    LauncherSchemaError,
    parse_utc_timestamp,
    utc_timestamp,
    validate_claim,
    validate_progress,
    validate_run_id,
    validate_status,
    validate_submission,
)
from .state import (
    ClaimEpochMismatchError,
    InvalidTransitionError,
    RevisionConflictError,
    revise_status_metadata,
    transition_status,
)

if TYPE_CHECKING:
    from .repository import LauncherRepository


class LauncherRunControl:
    """Guarded operations for one durable launcher control directory."""

    def __init__(self, repository: LauncherRepository, run_id: str) -> None:
        self.repository = repository
        self.run_id = validate_run_id(run_id)

    @property
    def control_dir(self) -> Path:
        return self.repository.run_control_path(self.run_id)

    @property
    def guard_path(self) -> Path:
        return self.control_dir / ".control.guard"

    @property
    def submission_path(self) -> Path:
        return self.control_dir / "submission.json"

    @property
    def status_path(self) -> Path:
        return self.control_dir / "status.json"

    @property
    def progress_path(self) -> Path:
        return self.control_dir / "progress.jsonl"

    @property
    def claim_path(self) -> Path:
        return self.control_dir / "execution.claim"

    @property
    def claims_dir(self) -> Path:
        return self.control_dir / "claims"

    @contextmanager
    def guard(self) -> Iterator[None]:
        """Hold the short per-control metadata guard."""
        _assert_directory(self.control_dir, label="Launcher control directory")
        _assert_confined_path(
            self.repository.runs_root,
            self.control_dir,
            allow_missing=False,
        )
        with _CrossProcessLock(self.guard_path):
            yield

    def confined_path(self, relative_path: str, *, must_exist: bool = False) -> Path:
        """Resolve a normalized control-relative path without following symlinks."""
        relative = _validate_relative_path(relative_path)
        target = self.control_dir.joinpath(*relative.split("/"))
        _assert_confined_path(
            self.control_dir,
            target,
            allow_missing=not must_exist,
        )
        return target

    def read_submission(self) -> dict[str, Any]:
        """Read and strictly validate immutable submission metadata."""
        self.confined_path("submission.json", must_exist=True)
        try:
            payload = validate_submission(_read_json(self.submission_path))
        except LauncherSchemaError as error:
            raise LauncherCorruptionError(
                "Invalid launcher submission metadata."
            ) from error
        if payload["run_id"] != self.run_id:
            raise LauncherCorruptionError("Submission run ID does not match its path.")
        if payload["storage_root"] != str(self.repository.storage_root):
            raise LauncherCorruptionError(
                "Submission storage root does not match its repository."
            )
        return payload

    def read_status(self) -> dict[str, Any]:
        """Read and strictly validate current guarded status."""
        self.confined_path("status.json", must_exist=True)
        return self._read_status_unlocked()

    def _read_status_unlocked(self) -> dict[str, Any]:
        try:
            payload = validate_status(_read_json(self.status_path))
        except LauncherSchemaError as error:
            raise LauncherCorruptionError("Invalid launcher status metadata.") from error
        if payload["run_id"] != self.run_id:
            raise LauncherCorruptionError("Status run ID does not match its path.")
        return payload

    def transition(
        self,
        *,
        expected_revision: int,
        new_state: str,
        expected_claim_epoch: int | None = None,
        updates: Mapping[str, Any] | None = None,
        updated_at: str | None = None,
    ) -> dict[str, Any]:
        """Commit one guarded state compare-and-swap."""
        with self.guard():
            status = self._read_status_unlocked()
            if status["state"] == "prepared" and new_state == "starting":
                raise InvalidTransitionError(
                    "Startup must use claim_start to commit claim and status together."
                )
            if status["claim_epoch"] is not None:
                self._require_active_claim_epoch(status["claim_epoch"])
            next_status = transition_status(
                status,
                expected_revision=expected_revision,
                new_state=new_state,
                expected_claim_epoch=expected_claim_epoch,
                updated_at=updated_at,
                updates=updates,
            )
            _atomic_write_json(self.status_path, next_status)
            return next_status

    def request_cancel(
        self,
        *,
        expected_revision: int,
        expected_claim_epoch: int | None = None,
        requested_at: str | None = None,
    ) -> dict[str, Any]:
        """Linearize cancellation according to the current launcher state."""
        with self.guard():
            status = self._read_status_unlocked()
            if status["revision"] != expected_revision:
                raise RevisionConflictError(
                    f"Expected status revision {expected_revision}, "
                    f"found {status['revision']}."
                )
            state = status["state"]
            if state in {
                "finalizing",
                "succeeded",
                "failed",
                "cancelled",
                "lost",
                "cancel_requested",
            }:
                return status
            timestamp = requested_at or utc_timestamp()
            if state == "prepared":
                next_status = transition_status(
                    status,
                    expected_revision=expected_revision,
                    new_state="cancelled",
                    updated_at=timestamp,
                    updates={"cancel_requested_at": timestamp},
                )
            elif state in {"starting", "running"}:
                self._require_active_claim_epoch(status["claim_epoch"])
                next_status = transition_status(
                    status,
                    expected_revision=expected_revision,
                    new_state="cancel_requested",
                    expected_claim_epoch=expected_claim_epoch,
                    updated_at=timestamp,
                    updates={"cancel_requested_at": timestamp},
                )
            else:
                raise InvalidTransitionError(
                    f"Cancellation is not defined for state {state!r}."
                )
            _atomic_write_json(self.status_path, next_status)
            if next_status["state"] == "cancel_requested":
                self._create_cancel_marker_best_effort()
            return next_status

    def _create_cancel_marker_best_effort(self) -> None:
        marker = self.control_dir / "cancel_requested"
        flags = os.O_WRONLY | os.O_CREAT
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(marker, flags, 0o600)
            os.close(descriptor)
            _sync_directory(self.control_dir)
        except OSError:
            pass

    def cancellation_marker_exists(self) -> bool:
        """Return whether the best-effort wake-up marker exists safely."""
        marker = self.confined_path("cancel_requested")
        if not _path_exists(marker):
            return False
        if _is_symlink(marker) or not marker.is_file():
            raise LauncherCorruptionError("Cancellation marker is not a regular file.")
        return True

    def append_progress(
        self,
        *,
        kind: str,
        payload: Mapping[str, Any],
        timestamp: str | None = None,
    ) -> dict[str, Any]:
        """Allocate and durably append the next global progress sequence."""
        with self.guard():
            entries, complete_length = self._read_progress_unlocked()
            sequence = entries[-1]["sequence"] + 1 if entries else 1
            entry = validate_progress(
                {
                    "schema": PROGRESS_SCHEMA,
                    "run_id": self.run_id,
                    "sequence": sequence,
                    "timestamp": timestamp or utc_timestamp(),
                    "kind": kind,
                    "payload": dict(payload),
                }
            )
            encoded = _canonical_json_bytes(entry) + b"\n"
            self.confined_path("progress.jsonl", must_exist=True)
            flags = os.O_RDWR
            if hasattr(os, "O_CLOEXEC"):
                flags |= os.O_CLOEXEC
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            descriptor = os.open(self.progress_path, flags)
            try:
                os.ftruncate(descriptor, complete_length)
                os.lseek(descriptor, 0, os.SEEK_END)
                written = os.write(descriptor, encoded)
                if written != len(encoded):
                    raise LauncherStorageError("Short launcher progress write.")
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            return entry

    def read_progress(self) -> list[dict[str, Any]]:
        """Read complete progress entries, ignoring one incomplete tail."""
        self.confined_path("progress.jsonl", must_exist=True)
        entries, _ = self._read_progress_unlocked()
        return entries

    def _read_progress_unlocked(self) -> tuple[list[dict[str, Any]], int]:
        encoded = _read_bytes(self.progress_path)
        last_newline = encoded.rfind(b"\n")
        complete_length = last_newline + 1
        complete = encoded[:complete_length]
        entries: list[dict[str, Any]] = []
        for line_number, line in enumerate(complete.splitlines(), start=1):
            if not line:
                raise LauncherCorruptionError(
                    f"Blank complete progress line {line_number}."
                )
            try:
                entry = validate_progress(
                    _decode_json(line, path=self.progress_path)
                )
            except LauncherSchemaError as error:
                raise LauncherCorruptionError(
                    f"Invalid progress entry on line {line_number}."
                ) from error
            if entry["run_id"] != self.run_id:
                raise LauncherCorruptionError(
                    f"Progress line {line_number} has the wrong run ID."
                )
            expected = entries[-1]["sequence"] + 1 if entries else 1
            if entry["sequence"] != expected:
                raise LauncherCorruptionError(
                    f"Progress line {line_number} has non-monotonic sequence."
                )
            entries.append(entry)
        return entries, complete_length

    def read_claim(self) -> dict[str, Any] | None:
        """Read the current execution lease, if one exists."""
        with self.guard():
            return self._read_claim_unlocked()

    def _read_claim_unlocked(self) -> dict[str, Any] | None:
        if not _path_exists(self.claim_path):
            return None
        self.confined_path("execution.claim", must_exist=True)
        try:
            claim = validate_claim(_read_json(self.claim_path))
        except LauncherSchemaError as error:
            raise LauncherCorruptionError("Invalid execution claim.") from error
        if claim["run_id"] != self.run_id:
            raise LauncherCorruptionError("Claim run ID does not match its path.")
        return claim

    def claim_start(
        self,
        *,
        expected_revision: int,
        owner: str,
        backend: str,
        lease_seconds: float,
        now: datetime | None = None,
        backend_absent_confirmed: bool = False,
    ) -> ClaimResult:
        """Acquire startup and atomically commit ``prepared -> starting``."""
        lease = _require_positive_lease(lease_seconds)
        current_time = _normalized_now(now)
        with self.guard():
            status = self._read_status_unlocked()
            if status["revision"] != expected_revision:
                raise RevisionConflictError(
                    f"Expected status revision {expected_revision}, "
                    f"found {status['revision']}."
                )
            if status["state"] != "prepared":
                raise InvalidTransitionError(
                    f"Startup claim requires prepared state, found {status['state']!r}."
                )
            if current_time < parse_utc_timestamp(
                status["updated_at"],
                field="updated_at",
            ):
                raise ClaimConflictError(
                    "Claim time precedes the current status revision."
                )
            previous = self._read_claim_unlocked()
            if previous is not None:
                expires = parse_utc_timestamp(
                    previous["expires_at"],
                    field="expires_at",
                )
                if expires > current_time:
                    if (
                        previous["owner"] == owner
                        and previous["backend"] == backend
                    ):
                        next_status = transition_status(
                            status,
                            expected_revision=expected_revision,
                            new_state="starting",
                            updated_at=utc_timestamp(current_time),
                            updates={
                                "claim_epoch": previous["epoch"],
                                "orchestrator": owner,
                            },
                        )
                        _atomic_write_json(self.status_path, next_status)
                        return ClaimResult(claim=previous, status=next_status)
                    raise ClaimConflictError(
                        f"Run is claimed by {previous['owner']!r} through "
                        f"{previous['expires_at']}."
                    )
                if not backend_absent_confirmed:
                    raise ClaimConflictError(
                        "Expired startup claim requires backend-absence confirmation."
                    )
                self._archive_claim_unlocked(previous)
            epoch = self._next_claim_epoch_unlocked(previous)
            claim = self._new_claim(
                owner=owner,
                backend=backend,
                epoch=epoch,
                lease_seconds=lease,
                now=current_time,
            )
            _atomic_write_json(self.claim_path, claim)
            next_status = transition_status(
                status,
                expected_revision=expected_revision,
                new_state="starting",
                updated_at=utc_timestamp(current_time),
                updates={"claim_epoch": epoch, "orchestrator": owner},
            )
            _atomic_write_json(self.status_path, next_status)
            return ClaimResult(claim=claim, status=next_status)

    def takeover_claim(
        self,
        *,
        expected_revision: int,
        expected_claim_epoch: int,
        owner: str,
        backend: str,
        lease_seconds: float,
        backend_absent_confirmed: bool,
        now: datetime | None = None,
    ) -> ClaimResult:
        """Replace an expired post-start claim for recovery, never for rerun."""
        if not backend_absent_confirmed:
            raise ClaimConflictError(
                "Claim takeover requires backend-absence confirmation."
            )
        lease = _require_positive_lease(lease_seconds)
        current_time = _normalized_now(now)
        with self.guard():
            status = self._read_status_unlocked()
            if status["state"] == "prepared":
                raise InvalidTransitionError(
                    "Prepared startup recovery must use claim_start."
                )
            if status["state"] in {"succeeded", "failed", "cancelled", "lost"}:
                raise InvalidTransitionError(
                    f"Terminal launcher state {status['state']!r} is immutable."
                )
            if status["revision"] != expected_revision:
                raise RevisionConflictError(
                    f"Expected status revision {expected_revision}, "
                    f"found {status['revision']}."
                )
            if status["claim_epoch"] != expected_claim_epoch:
                raise ClaimEpochMismatchError(
                    f"Expected claim epoch {expected_claim_epoch}, "
                    f"found {status['claim_epoch']}."
                )
            if current_time < parse_utc_timestamp(
                status["updated_at"],
                field="updated_at",
            ):
                raise ClaimConflictError(
                    "Claim time precedes the current status revision."
                )
            previous = self._read_claim_unlocked()
            if previous is None:
                raise LauncherCorruptionError(
                    "Claimed launcher status has no execution.claim."
                )
            if (
                previous["epoch"] == expected_claim_epoch + 1
                and status["claim_epoch"] == expected_claim_epoch
                and previous["owner"] == owner
                and previous["backend"] == backend
            ):
                expires = parse_utc_timestamp(
                    previous["expires_at"],
                    field="expires_at",
                )
                if expires <= current_time:
                    self._archive_claim_unlocked(previous)
                    epoch = self._next_claim_epoch_unlocked(previous)
                    previous = self._new_claim(
                        owner=owner,
                        backend=backend,
                        epoch=epoch,
                        lease_seconds=lease,
                        now=current_time,
                    )
                    _atomic_write_json(self.claim_path, previous)
                next_status = revise_status_metadata(
                    status,
                    expected_revision=expected_revision,
                    expected_claim_epoch=expected_claim_epoch,
                    updated_at=utc_timestamp(current_time),
                    updates={
                        "claim_epoch": previous["epoch"],
                        "orchestrator": owner,
                    },
                )
                _atomic_write_json(self.status_path, next_status)
                return ClaimResult(claim=previous, status=next_status)
            if previous["epoch"] != expected_claim_epoch:
                raise ClaimEpochMismatchError(
                    f"Expected claim epoch {expected_claim_epoch}, "
                    f"found {previous['epoch']}."
                )
            expires = parse_utc_timestamp(
                previous["expires_at"],
                field="expires_at",
            )
            if expires > current_time:
                raise ClaimConflictError("Execution claim is still live.")
            self._archive_claim_unlocked(previous)
            epoch = self._next_claim_epoch_unlocked(previous)
            claim = self._new_claim(
                owner=owner,
                backend=backend,
                epoch=epoch,
                lease_seconds=lease,
                now=current_time,
            )
            _atomic_write_json(self.claim_path, claim)
            next_status = revise_status_metadata(
                status,
                expected_revision=expected_revision,
                expected_claim_epoch=expected_claim_epoch,
                updated_at=utc_timestamp(current_time),
                updates={"claim_epoch": epoch, "orchestrator": owner},
            )
            _atomic_write_json(self.status_path, next_status)
            return ClaimResult(claim=claim, status=next_status)

    def heartbeat_claim(
        self,
        *,
        expected_epoch: int,
        expected_nonce: str,
        lease_seconds: float,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Renew the current lease if its epoch, nonce, and expiry still match."""
        lease = _require_positive_lease(lease_seconds)
        current_time = _normalized_now(now)
        with self.guard():
            claim = self._read_claim_unlocked()
            if claim is None:
                raise LauncherCorruptionError("Execution claim is missing.")
            if claim["epoch"] != expected_epoch:
                raise ClaimEpochMismatchError(
                    f"Expected claim epoch {expected_epoch}, found {claim['epoch']}."
                )
            if claim["nonce"] != expected_nonce:
                raise ClaimConflictError("Execution claim nonce does not match.")
            heartbeat = parse_utc_timestamp(
                claim["heartbeat_at"],
                field="heartbeat_at",
            )
            expires = parse_utc_timestamp(claim["expires_at"], field="expires_at")
            if expires <= current_time:
                raise ClaimExpiredError("Execution claim has expired.")
            if current_time <= heartbeat:
                raise ClaimConflictError(
                    "Execution claim heartbeat time must advance monotonically."
                )
            next_expiry = max(
                expires,
                current_time + timedelta(seconds=lease),
            )
            renewed = dict(claim)
            renewed["heartbeat_at"] = utc_timestamp(current_time)
            renewed["expires_at"] = utc_timestamp(next_expiry)
            renewed = validate_claim(renewed)
            _atomic_write_json(self.claim_path, renewed)
            return renewed

    def read_claim_history(self) -> list[dict[str, Any]]:
        """Read append-only superseded claims ordered by epoch."""
        with self.guard():
            self.confined_path("claims", must_exist=True)
            claims: list[dict[str, Any]] = []
            for path in self.claims_dir.iterdir():
                if _is_symlink(path) or not path.is_file():
                    raise LauncherCorruptionError(
                        f"Unsafe claim-history entry: {path.name!r}."
                    )
                if not path.name.endswith(".json"):
                    raise LauncherCorruptionError(
                        f"Unknown claim-history entry: {path.name!r}."
                    )
                try:
                    epoch = int(path.stem)
                except ValueError as error:
                    raise LauncherCorruptionError(
                        f"Invalid claim-history filename: {path.name!r}."
                    ) from error
                try:
                    claim = validate_claim(_read_json(path))
                except LauncherSchemaError as error:
                    raise LauncherCorruptionError(
                        f"Invalid claim-history file: {path.name!r}."
                    ) from error
                if claim["run_id"] != self.run_id or claim["epoch"] != epoch:
                    raise LauncherCorruptionError(
                        f"Claim-history identity mismatch: {path.name!r}."
                    )
                claims.append(claim)
            claims.sort(key=lambda item: item["epoch"])
            if any(
                later["epoch"] <= earlier["epoch"]
                for earlier, later in zip(claims, claims[1:])
            ):
                raise LauncherCorruptionError("Claim-history epochs are not monotonic.")
            return claims

    def _new_claim(
        self,
        *,
        owner: str,
        backend: str,
        epoch: int,
        lease_seconds: float,
        now: datetime,
    ) -> dict[str, Any]:
        return validate_claim(
            {
                "schema": CLAIM_SCHEMA,
                "run_id": self.run_id,
                "owner": owner,
                "backend": backend,
                "nonce": uuid.uuid4().hex,
                "epoch": epoch,
                "created_at": utc_timestamp(now),
                "heartbeat_at": utc_timestamp(now),
                "expires_at": utc_timestamp(
                    now + timedelta(seconds=lease_seconds)
                ),
            }
        )

    def _archive_claim_unlocked(self, claim: Mapping[str, Any]) -> None:
        _assert_directory(self.claims_dir, label="Claim-history directory")
        history_path = self.claims_dir / f"{claim['epoch']}.json"
        if _path_exists(history_path):
            existing = validate_claim(_read_json(history_path))
            if existing != dict(claim):
                raise LauncherCorruptionError(
                    f"Claim history epoch {claim['epoch']} already differs."
                )
            return
        try:
            _atomic_create_json(history_path, claim)
        except FileExistsError:
            existing = validate_claim(_read_json(history_path))
            if existing != dict(claim):
                raise LauncherCorruptionError(
                    f"Claim history epoch {claim['epoch']} raced with different data."
                )

    def _next_claim_epoch_unlocked(
        self,
        current: Mapping[str, Any] | None,
    ) -> int:
        highest = int(current["epoch"]) if current is not None else 0
        _assert_directory(self.claims_dir, label="Claim-history directory")
        for path in self.claims_dir.iterdir():
            if path.name.endswith(".json") and path.stem.isdigit():
                highest = max(highest, int(path.stem))
        return highest + 1

    def _require_active_claim_epoch(self, epoch: object) -> dict[str, Any]:
        if not isinstance(epoch, int):
            raise LauncherCorruptionError(
                "Claimed launcher status has no valid claim epoch."
            )
        claim = self._read_claim_unlocked()
        if claim is None:
            raise LauncherCorruptionError(
                "Claimed launcher status has no execution.claim."
            )
        if claim["epoch"] != epoch:
            raise LauncherCorruptionError(
                "Status claim epoch does not match execution.claim."
            )
        return claim
