"""Allocation and filesystem primitives for launcher control state."""

from __future__ import annotations

import errno
import json
import math
import os
import shutil
import stat
import sys
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, BinaryIO

from bioimageflow.storage.allocation import RunAllocationLock

from .errors import LauncherProtocolError, LauncherStateConflictError
from .schemas import (
    STATUS_SCHEMA,
    LauncherSchemaError,
    validate_run_id,
    validate_status,
    validate_submission,
)

if TYPE_CHECKING:
    from .control import LauncherRunControl

if sys.platform == "win32":
    import msvcrt
else:
    import fcntl


class LauncherStorageError(LauncherProtocolError):
    """Base class for launcher storage protocol failures."""


class LauncherCorruptionError(LauncherStorageError):
    """Raised when durable launcher state is malformed or unsafe."""


class RunAlreadyExistsError(LauncherStateConflictError):
    """Raised when either run namespace already owns a candidate ID."""


class RunNotFoundError(LauncherStorageError):
    """Raised when a submitted-run control directory does not exist."""


class ClaimConflictError(LauncherStateConflictError):
    """Raised when another live execution lease excludes the caller."""


class ClaimExpiredError(LauncherStateConflictError):
    """Raised when an owner tries to renew an expired execution lease."""


@dataclass(frozen=True)
class ClaimResult:
    """The claim and status committed by one guarded claim operation."""

    claim: dict[str, Any]
    status: dict[str, Any]


def _canonical_json_bytes(payload: Mapping[str, Any]) -> bytes:
    try:
        return json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise LauncherSchemaError("Launcher metadata must be finite JSON.") from error


def _sync_directory(path: Path) -> None:
    if sys.platform == "win32":
        return
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    encoded = _canonical_json_bytes(payload)
    temporary = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    descriptor = os.open(temporary, flags, 0o600)
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.close(descriptor)
        descriptor = -1
        os.replace(temporary, path)
        _sync_directory(path.parent)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _atomic_create_json(path: Path, payload: Mapping[str, Any]) -> None:
    """Install an immutable JSON file without replacing an existing path."""
    encoded = _canonical_json_bytes(payload)
    temporary = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    descriptor = os.open(temporary, flags, 0o600)
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.close(descriptor)
        descriptor = -1
        os.link(temporary, path)
        _sync_directory(path.parent)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise LauncherCorruptionError(f"Duplicate JSON object key {key!r}.")
        result[key] = value
    return result


def _reject_nonfinite_constant(value: str) -> None:
    raise LauncherCorruptionError(f"Non-finite JSON constant {value!r}.")


def _decode_json(encoded: bytes, *, path: Path) -> dict[str, Any]:
    try:
        value = json.loads(
            encoded.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_nonfinite_constant,
        )
    except LauncherCorruptionError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise LauncherCorruptionError(f"Malformed JSON in {path}.") from error
    if not isinstance(value, dict):
        raise LauncherCorruptionError(f"Expected a JSON object in {path}.")
    return value


def _open_binary_no_follow(path: Path) -> BinaryIO:
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        if error.errno == errno.ELOOP:
            raise LauncherCorruptionError(
                f"Symlink file is forbidden: {path}."
            ) from error
        raise
    if not stat.S_ISREG(os.fstat(descriptor).st_mode):
        os.close(descriptor)
        raise LauncherCorruptionError(f"Expected regular file: {path}.")
    return os.fdopen(descriptor, "rb")


def _read_bytes(path: Path) -> bytes:
    try:
        with _open_binary_no_follow(path) as stream:
            return stream.read()
    except FileNotFoundError:
        raise
    except LauncherCorruptionError:
        raise
    except OSError as error:
        raise LauncherStorageError(f"Could not read launcher file {path}.") from error


def _read_json(path: Path) -> dict[str, Any]:
    return _decode_json(_read_bytes(path), path=path)


def _is_symlink(path: Path) -> bool:
    try:
        return stat.S_ISLNK(path.lstat().st_mode)
    except FileNotFoundError:
        return False


def _path_exists(path: Path) -> bool:
    return os.path.lexists(path)


def _assert_directory(path: Path, *, label: str) -> None:
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError as error:
        raise RunNotFoundError(f"{label} does not exist: {path}.") from error
    if stat.S_ISLNK(mode):
        raise LauncherCorruptionError(f"{label} must not be a symlink: {path}.")
    if not stat.S_ISDIR(mode):
        raise LauncherCorruptionError(f"{label} must be a directory: {path}.")


def _ensure_secure_directory(root: Path, target: Path) -> None:
    try:
        relative = target.relative_to(root)
    except ValueError as error:
        raise LauncherCorruptionError(f"Path escapes storage root: {target}.") from error
    root.mkdir(parents=True, exist_ok=True)
    if _is_symlink(root) or not root.is_dir():
        raise LauncherCorruptionError(f"Storage root is not a safe directory: {root}.")
    current = root
    for part in relative.parts:
        current = current / part
        try:
            current.mkdir(mode=0o700)
        except FileExistsError:
            pass
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError as error:
            raise LauncherCorruptionError(
                f"Directory disappeared during validation: {current}."
            ) from error
        if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
            raise LauncherCorruptionError(f"Unsafe directory component: {current}.")


def _validate_relative_path(value: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("Relative launcher path must be a non-empty string.")
    if "\x00" in value or value.startswith("/") or "\\" in value:
        raise ValueError(f"Unsafe relative launcher path: {value!r}")
    if any(part in {"", ".", ".."} for part in value.split("/")):
        raise ValueError(f"Unsafe relative launcher path: {value!r}")
    return value


def _assert_confined_path(
    root: Path,
    target: Path,
    *,
    allow_missing: bool,
) -> None:
    try:
        relative = target.relative_to(root)
    except ValueError as error:
        raise LauncherCorruptionError(f"Path escapes launcher root: {target}.") from error
    _assert_directory(root, label="Launcher control directory")
    current = root
    for index, part in enumerate(relative.parts):
        current = current / part
        final = index == len(relative.parts) - 1
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            if allow_missing:
                return
            raise LauncherCorruptionError(
                f"Required launcher path is missing: {current}."
            )
        if stat.S_ISLNK(mode):
            raise LauncherCorruptionError(
                f"Symlink launcher path is forbidden: {current}."
            )
        if not final and not stat.S_ISDIR(mode):
            raise LauncherCorruptionError(
                f"Launcher path component is not a directory: {current}."
            )
    try:
        target.resolve(strict=False).relative_to(root.resolve(strict=True))
    except (OSError, ValueError) as error:
        raise LauncherCorruptionError(
            f"Launcher path escapes control root: {target}."
        ) from error


class _CrossProcessLock:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._descriptor: int | None = None

    def __enter__(self) -> "_CrossProcessLock":
        if _is_symlink(self._path):
            raise LauncherCorruptionError(
                f"Launcher guard must not be a symlink: {self._path}."
            )
        flags = os.O_RDWR | os.O_CREAT
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(self._path, flags, 0o600)
        except OSError as error:
            if error.errno == errno.ELOOP:
                raise LauncherCorruptionError(
                    f"Launcher guard must not be a symlink: {self._path}."
                ) from error
            raise
        if not stat.S_ISREG(os.fstat(descriptor).st_mode) or _is_symlink(self._path):
            os.close(descriptor)
            raise LauncherCorruptionError(
                f"Launcher guard must be a regular non-symlink file: {self._path}."
            )
        try:
            if sys.platform == "win32":
                if os.fstat(descriptor).st_size == 0:
                    os.write(descriptor, b"\0")
                    os.fsync(descriptor)
                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_LOCK, 1)
            else:
                fcntl.flock(descriptor, fcntl.LOCK_EX)
        except BaseException:
            os.close(descriptor)
            raise
        self._descriptor = descriptor
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        descriptor = self._descriptor
        self._descriptor = None
        if descriptor is None:
            return
        try:
            if sys.platform == "win32":
                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def _require_positive_lease(value: float) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or value <= 0
    ):
        raise ValueError("lease_seconds must be a positive finite number.")
    return float(value)


def _normalized_now(value: datetime | None) -> datetime:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("now must be timezone-aware.")
    return current.astimezone(timezone.utc)


class LauncherRepository:
    """Allocate and reopen submitted-run control directories."""

    def __init__(self, storage_root: str | Path) -> None:
        requested = Path(storage_root).expanduser()
        if not requested.is_absolute():
            requested = Path.cwd() / requested
        self.storage_root = requested.resolve(strict=False)

    @property
    def launcher_root(self) -> Path:
        return self.storage_root / "launcher" / "v1"

    @property
    def runs_root(self) -> Path:
        return self.launcher_root / "runs"

    @property
    def canonical_runs_root(self) -> Path:
        return self.storage_root / "views" / "runs"

    @property
    def allocation_guard_path(self) -> Path:
        return self.launcher_root / ".allocation.guard"

    @contextmanager
    def allocation_guard(self) -> Iterator[None]:
        """Hold the short storage-root allocation guard."""
        _ensure_secure_directory(self.storage_root, self.runs_root)
        with RunAllocationLock(self.storage_root):
            yield

    def new_run_id(self) -> str:
        """Return a fresh UUID4 run ID; allocation still detects collisions."""
        from .schemas import new_run_id

        return new_run_id()

    def run_control_path(self, run_id: str) -> Path:
        """Return the validated launcher control path for one run."""
        return self.runs_root / validate_run_id(run_id)

    def canonical_run_path(self, run_id: str) -> Path:
        """Return the validated canonical run-view path for one run."""
        return self.canonical_runs_root / validate_run_id(run_id)

    def create_candidate(self, run_id: str) -> Path:
        """Create a unique hidden sibling for staging a complete control tree."""
        safe_run_id = validate_run_id(run_id)
        _ensure_secure_directory(self.storage_root, self.runs_root)
        candidate = self.runs_root / f".{safe_run_id}.{uuid.uuid4().hex}.tmp"
        candidate.mkdir(mode=0o700)
        _sync_directory(self.runs_root)
        return candidate

    def allocate(
        self,
        submission: Mapping[str, Any],
        *,
        backend: str,
        candidate_dir: str | Path | None = None,
        allocation_guard_held: bool = False,
    ) -> LauncherRunControl:
        """Atomically install one complete revision-zero control directory.

        A supplied candidate must have been returned by :meth:`create_candidate`.
        Allocation consumes it, making staged inputs and metadata visible together.
        """
        from .control import LauncherRunControl

        if type(allocation_guard_held) is not bool:
            raise TypeError("allocation_guard_held must be a bool.")

        validated = validate_submission(submission)
        run_id = validated["run_id"]
        if not isinstance(backend, str) or not backend:
            raise ValueError("backend must be a non-empty string.")
        if validated["storage_root"] != str(self.storage_root):
            raise LauncherSchemaError(
                "submission.storage_root does not match this launcher repository."
            )
        control_dir = self.run_control_path(run_id)
        canonical_dir = self.canonical_run_path(run_id)
        candidate = (
            self.create_candidate(run_id)
            if candidate_dir is None
            else self._validate_candidate(run_id, Path(candidate_dir))
        )
        try:
            for protected in (
                "submission.json",
                "status.json",
                "progress.jsonl",
                "execution.claim",
                "claims",
                "cancel_requested",
                "error.json",
                "command.json",
                "local_process.json",
                "local_process_exit.json",
                "logs",
                "return",
                "result_export.json",
                "retry_transaction.json",
                ".control.guard",
            ):
                if _path_exists(candidate / protected):
                    raise LauncherStorageError(
                        f"Staged candidate must not provide protected path {protected!r}."
                    )
            self._validate_candidate_tree(candidate)
            timestamp = validated["created_at"]
            status = validate_status(
                {
                    "schema": STATUS_SCHEMA,
                    "run_id": run_id,
                    "state": "prepared",
                    "revision": 0,
                    "created_at": timestamp,
                    "updated_at": timestamp,
                    "backend": backend,
                    "orchestrator": None,
                    "claim_epoch": None,
                    "cancel_requested_at": None,
                    "hard_termination_requested": False,
                    "error": None,
                }
            )
            _atomic_write_json(candidate / "submission.json", validated)
            _atomic_write_json(candidate / "status.json", status)
            progress = candidate / "progress.jsonl"
            descriptor = os.open(
                progress,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            (candidate / "claims").mkdir(mode=0o700)
            _sync_directory(candidate)
            def install() -> None:
                if _path_exists(control_dir) or _path_exists(canonical_dir):
                    raise RunAlreadyExistsError(
                        f"Run ID {run_id!r} already exists in a run namespace."
                    )
                candidate.rename(control_dir)
                _sync_directory(self.runs_root)

            if allocation_guard_held:
                install()
            else:
                with self.allocation_guard():
                    install()
        except BaseException:
            if candidate.exists() and not candidate.is_symlink():
                shutil.rmtree(candidate)
            raise
        return LauncherRunControl(self, run_id)

    create_run = allocate

    def open(self, run_id: str) -> LauncherRunControl:
        """Open an existing submitted-run control directory."""
        from .control import LauncherRunControl

        control = LauncherRunControl(self, run_id)
        _assert_directory(control.control_dir, label="Launcher control directory")
        _assert_confined_path(
            self.runs_root,
            control.control_dir,
            allow_missing=False,
        )
        control.read_submission()
        control.read_status()
        return control

    open_run = open

    def _validate_candidate(self, run_id: str, candidate: Path) -> Path:
        candidate = candidate.absolute()
        expected_prefix = f".{run_id}."
        try:
            candidate.relative_to(self.runs_root)
        except ValueError as error:
            raise LauncherStorageError(
                "Launcher candidate must be a runs-root sibling."
            ) from error
        if (
            candidate.parent != self.runs_root
            or not candidate.name.startswith(expected_prefix)
            or not candidate.name.endswith(".tmp")
        ):
            raise LauncherStorageError(
                "Launcher candidate was not created for this run."
            )
        _assert_directory(candidate, label="Launcher candidate directory")
        if candidate.resolve(strict=True).parent != self.runs_root.resolve(strict=True):
            raise LauncherCorruptionError("Launcher candidate escapes the runs root.")
        return candidate

    def _validate_candidate_tree(self, candidate: Path) -> None:
        for directory, names, files in os.walk(candidate, followlinks=False):
            directory_path = Path(directory)
            if _is_symlink(directory_path):
                raise LauncherCorruptionError(
                    f"Staged candidate contains a symlink: {directory_path}."
                )
            for name in [*names, *files]:
                path = directory_path / name
                if _is_symlink(path):
                    raise LauncherCorruptionError(
                        f"Staged candidate contains a symlink: {path}."
                    )
