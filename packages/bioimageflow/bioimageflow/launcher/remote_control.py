"""Cluster-local handlers for bounded remote run observation and control."""

from __future__ import annotations

import base64
import os
import stat
from pathlib import Path
from typing import Any

from .cluster_protocol import ClusterProtocolFailure
from .cluster_upload import _ensure_root, _receipt, normalized_root
from .artifacts import read_error
from .repository import LauncherCorruptionError, RunNotFoundError
from .run import WorkflowRun
from .schemas import validate_run_id
from .retry import RecomputeRequest, RunRetryPlan, prepare_retry_plan, submit_retry_plan
from .errors import PSIJSubmissionUncertainError, WorkflowRunRetryError

MAX_PROGRESS_PAGE = 500
MAX_LOG_CHUNK = 256 * 1024
_TERMINAL = frozenset({"succeeded", "failed", "cancelled", "lost"})


def _storage_path(value: Any) -> Path:
    path = normalized_root(value)
    return path


def _open(storage_path: Any, run_id: Any) -> WorkflowRun:
    try:
        canonical = validate_run_id(run_id)
        return WorkflowRun.open(_storage_path(storage_path), canonical)
    except RunNotFoundError as exc:
        raise ClusterProtocolFailure(
            "run-not-found",
            "The requested workflow run does not exist.",
        ) from exc
    except (LauncherCorruptionError, TypeError, ValueError) as exc:
        raise ClusterProtocolFailure(
            "invalid-run",
            "The requested workflow run is invalid or corrupt.",
        ) from exc


def _observation(run: WorkflowRun) -> dict[str, Any]:
    submission = run._control.read_submission()
    status = run._control.read_status()
    error: dict[str, Any] | None = None
    if status["state"] in {"failed", "lost"}:
        try:
            persisted = read_error(run._control)
            error = {
                "code": persisted["code"],
                "exception_type": persisted["exception_type"],
                "message": persisted["message"],
                "run_id": persisted["run_id"],
            }
        except Exception:
            error = None
    result = {
        "error": error,
        "run_id": run.id,
        "state": status["state"],
        "status_revision": status["revision"],
        "storage_path": submission["storage_root"],
        "submission_schema": submission["schema"],
        "status_schema": status["schema"],
        "terminal": status["state"] in _TERMINAL,
        "updated_at": status["updated_at"],
    }
    if submission["schema"] == "bioimageflow.launcher.submission.v3":
        result["retry_plan"] = submission["retry_plan"]
    return result


def inspect_run(storage_path: Any, run_id: Any) -> dict[str, Any]:
    """Validate and observe one run without mutating launcher state."""
    return _observation(_open(storage_path, run_id))


def refresh_run(storage_path: Any, run_id: Any) -> dict[str, Any]:
    """Delegate authorized reconciliation to the local WorkflowRun."""
    run = _open(storage_path, run_id)
    run.refresh()
    return _observation(run)


def read_progress_page(
    storage_path: Any,
    run_id: Any,
    after_sequence: Any,
    limit: Any,
) -> dict[str, Any]:
    if type(after_sequence) is not int or after_sequence < 0:
        raise ClusterProtocolFailure(
            "invalid-progress-cursor",
            "after_sequence must be a non-negative integer.",
        )
    if type(limit) is not int or not 1 <= limit <= MAX_PROGRESS_PAGE:
        raise ClusterProtocolFailure(
            "invalid-progress-limit",
            f"limit must be between 1 and {MAX_PROGRESS_PAGE}.",
        )
    run = _open(storage_path, run_id)
    page, has_more = run._control.read_progress_page(
        after_sequence=after_sequence,
        limit=limit,
    )
    next_sequence = after_sequence if not page else page[-1]["sequence"]
    return {
        **_observation(run),
        "events": page,
        "has_more": has_more,
        "next_sequence": next_sequence,
    }


def _log_path(run: WorkflowRun, stream: Any) -> Path:
    if stream not in {"stdout", "stderr"}:
        raise ClusterProtocolFailure(
            "invalid-log-stream",
            "stream must be stdout or stderr.",
        )
    name = "orchestrator.out" if stream == "stdout" else "orchestrator.err"
    return run._control.confined_path(f"logs/{name}")


def read_log_page(
    storage_path: Any,
    run_id: Any,
    stream: Any,
    offset: Any,
    identity: Any,
    snapshot_size: Any,
    limit: Any,
) -> dict[str, Any]:
    if type(offset) is not int or offset < 0:
        raise ClusterProtocolFailure(
            "invalid-log-offset",
            "offset must be a non-negative integer.",
        )
    if identity is not None and (type(identity) is not str or not identity):
        raise ClusterProtocolFailure(
            "invalid-log-identity",
            "identity must be null or a non-empty string.",
        )
    if snapshot_size is not None and (
        type(snapshot_size) is not int or snapshot_size < 0
    ):
        raise ClusterProtocolFailure(
            "invalid-log-snapshot",
            "snapshot_size must be null or a non-negative integer.",
        )
    if (identity is None) != (snapshot_size is None) or (
        identity is None and offset != 0
    ):
        raise ClusterProtocolFailure(
            "invalid-log-snapshot",
            "Log identity, snapshot size, and offset are inconsistent.",
        )
    if type(limit) is not int or not 1 <= limit <= MAX_LOG_CHUNK:
        raise ClusterProtocolFailure(
            "invalid-log-limit",
            f"limit must be between 1 and {MAX_LOG_CHUNK}.",
        )
    run = _open(storage_path, run_id)
    path = _log_path(run, stream)
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        return {
            **_observation(run),
            "data": "",
            "eof": True,
            "exists": False,
            "identity": None,
            "next_offset": 0,
            "reset": offset != 0 or identity is not None,
            "snapshot_size": 0,
            "stream": stream,
        }
    except OSError as exc:
        raise ClusterProtocolFailure(
            "unsafe-log",
            "The requested launcher log is unavailable.",
        ) from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ClusterProtocolFailure(
                "unsafe-log",
                "The requested launcher log is not a regular file.",
            )
        current_identity = f"{metadata.st_dev:x}:{metadata.st_ino:x}"
        reset = (
            (identity is not None and identity != current_identity)
            or offset > metadata.st_size
            or (
                snapshot_size is not None
                and metadata.st_size < snapshot_size
            )
        )
        start = 0 if reset else offset
        end = metadata.st_size if snapshot_size is None or reset else snapshot_size
        os.lseek(descriptor, start, os.SEEK_SET)
        data = os.read(descriptor, min(limit, max(0, end - start)))
        next_offset = start + len(data)
        eof = next_offset >= end
    finally:
        os.close(descriptor)
    return {
        **_observation(run),
        "data": base64.b64encode(data).decode("ascii"),
        "eof": eof,
        "exists": True,
        "identity": current_identity,
        "next_offset": next_offset,
        "reset": reset,
        "snapshot_size": end,
        "stream": stream,
    }


def cancel_run(
    staging_root: Any,
    storage_path: Any,
    run_id: Any,
    request_id: str,
    request_digest: str,
) -> dict[str, Any]:
    root = normalized_root(staging_root)
    _ensure_root(root)

    def mutate() -> dict[str, Any]:
        run = _open(storage_path, run_id)
        run.cancel()
        run.refresh()
        return _observation(run)

    return _receipt(root, "cancel", request_id, request_digest, mutate)


def prepare_run_retry(storage_path: Any, run_id: Any, recompute: Any) -> dict[str, Any]:
    """Preview one retained retry without invalidating or allocating."""
    try:
        request = None if recompute is None else RecomputeRequest.from_dict(recompute)
        return prepare_retry_plan(_open(storage_path, run_id), request).to_dict()
    except WorkflowRunRetryError as exc:
        raise ClusterProtocolFailure("retry-conflict", str(exc)) from exc
    except (TypeError, ValueError) as exc:
        raise ClusterProtocolFailure("invalid-retry", "Retry request is invalid.") from exc


def submit_run_retry(storage_path: Any, plan: Any) -> dict[str, Any]:
    """Apply one immutable retry plan through cluster-local storage APIs."""
    try:
        parsed = RunRetryPlan.from_dict(plan)
        run = submit_retry_plan(
            _open(storage_path, parsed.parent_run_id),
            parsed,
        )
        return _observation(run)
    except PSIJSubmissionUncertainError as exc:
        raise ClusterProtocolFailure(
            "retry-submission-uncertain",
            "Retry scheduler submission outcome is uncertain; it was not resubmitted.",
        ) from exc
    except WorkflowRunRetryError as exc:
        raise ClusterProtocolFailure("retry-conflict", str(exc)) from exc
    except (TypeError, ValueError) as exc:
        raise ClusterProtocolFailure("invalid-retry", "Retry request is invalid.") from exc
