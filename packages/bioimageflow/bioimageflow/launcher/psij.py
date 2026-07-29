"""Lazy PSI/J adapter for one reconnectable scheduler orchestrator job."""

from __future__ import annotations

import importlib
import stat
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any

from .artifacts import build_error_payload
from .control import LauncherRunControl
from .errors import (
    BackendNotSupportedError,
    LauncherProtocolError,
    PSIJSubmissionUncertainError,
)
from .psij_artifacts import (
    read_intent,
    read_receipt,
    validate_native_id,
    write_intent,
    write_receipt,
)
from .state import RevisionConflictError
from .types import PSIJLaunchConfig


_TERMINAL_STATES = frozenset({"COMPLETED", "FAILED", "CANCELED"})
_STATE_EVENTS = {
    "QUEUED": "psij_queued",
    "ACTIVE": "psij_active",
    "COMPLETED": "psij_completed",
    "FAILED": "psij_failed",
    "CANCELED": "psij_cancelled",
}


def _event_for_state(state: str | None) -> str:
    if state is None:
        return "psij_unknown"
    return _STATE_EVENTS.get(state, "psij_unknown")


@dataclass(frozen=True, slots=True)
class PSIJLaunch:
    """One PSI/J job submitted for a prepared orchestrator run."""

    native_id: str
    executor_name: str
    executor_work_dir: Path
    job: Any = field(repr=False, compare=False)
    executor: Any = field(repr=False, compare=False)
    backend: str = "psij"


@dataclass(frozen=True, slots=True)
class PSIJObservation:
    """Normalized, secondary scheduler observation."""

    executor: str
    native_id: str
    state: str | None

    @property
    def final(self) -> bool:
        return self.state in _TERMINAL_STATES


def _load_runtime() -> Any:
    try:
        return importlib.import_module("psij")
    except ImportError as error:
        raise BackendNotSupportedError(
            "PSI/J launcher support is not installed; install "
            "'bioimageflow[psij]'.",
            details={"backend": "psij", "extra": "psij"},
        ) from error


def _safe_directory(path: Path, *, label: str) -> Path:
    try:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        mode = path.lstat().st_mode
    except OSError as error:
        raise LauncherProtocolError(f"Could not prepare {label}.") from error
    if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
        raise LauncherProtocolError(f"{label} must be a non-symlink directory.")
    return path


def _executor_work_dir(control: LauncherRunControl) -> Path:
    return _safe_directory(
        control.confined_path("psij/executor"),
        label="PSI/J executor work directory",
    )


def _executor(runtime: Any, name: str, work_dir: Path) -> Any:
    try:
        names = runtime.JobExecutor.get_executor_names()
    except Exception as error:
        raise BackendNotSupportedError(
            "PSI/J executor descriptors could not be inspected.",
            details={"backend": "psij", "executor": name},
        ) from error
    if name not in names:
        raise BackendNotSupportedError(
            f"PSI/J executor descriptor {name!r} is unavailable.",
            details={"backend": "psij", "executor": name},
        )
    try:
        config = runtime.JobExecutorConfig(work_directory=work_dir)
        return runtime.JobExecutor.get_instance(name, config=config)
    except Exception as error:
        raise BackendNotSupportedError(
            f"PSI/J executor {name!r} could not be constructed.",
            details={"backend": "psij", "executor": name},
        ) from error


def _build_spec(runtime: Any, job: dict[str, Any]) -> Any:
    resources = runtime.ResourceSpecV1(**job["resources"])
    attributes = runtime.JobAttributes(
        duration=timedelta(seconds=job["attributes"]["duration_seconds"]),
        queue_name=job["attributes"]["queue_name"],
        account=job["attributes"]["account"],
    )
    return runtime.JobSpec(
        executable=job["executable"],
        arguments=list(job["arguments"]),
        directory=job["directory"],
        name=job["name"],
        stdout_path=Path(job["stdout_path"]),
        stderr_path=Path(job["stderr_path"]),
        resources=resources,
        attributes=attributes,
    )


def _state_name(state: object) -> str | None:
    name = getattr(state, "name", None)
    if type(name) is str:
        return name.upper()
    encoded = str(state)
    if encoded.startswith("JobState."):
        return encoded.rsplit(".", 1)[-1].upper()
    upper = encoded.upper()
    return upper if upper in {"NEW", *_STATE_EVENTS} else None


def _verify_attached_id(job: Any, expected: str) -> None:
    if job.native_id is None:
        return
    attached_id = validate_native_id(job.native_id)
    if attached_id != expected:
        raise LauncherProtocolError("PSI/J attached a different native job ID.")


def _append_observation(
    control: LauncherRunControl,
    *,
    event: str,
    executor: str,
    native_id: str | None,
    state: str | None,
    message: str | None,
) -> None:
    payload = {
        "schema": "bioimageflow.launcher.backend_event.v1",
        "event": event,
        "executor": executor,
        "native_id": native_id,
        "state": state,
        "message": message,
    }
    progress = control.read_progress()
    if progress and progress[-1]["kind"] == "backend":
        if progress[-1]["payload"] == payload:
            return
    control.append_progress(kind="backend", payload=payload)


def _uncertain(
    control: LauncherRunControl,
    *,
    executor: str,
    cause: BaseException | None = None,
) -> PSIJSubmissionUncertainError:
    _append_observation(
        control,
        event="psij_submission_uncertain",
        executor=executor,
        native_id=None,
        state=None,
        message="PSI/J submission outcome is uncertain.",
    )
    error = PSIJSubmissionUncertainError(
        "PSI/J submission may have succeeded, but no durable native job "
        "receipt exists; this run will not be submitted again.",
        details={"run_id": control.run_id, "executor": executor},
    )
    if cause is not None:
        error.__cause__ = cause
    return error


def launch_psij(
    control: LauncherRunControl,
    launch: PSIJLaunchConfig,
) -> PSIJLaunch:
    """Submit exactly one PSI/J job, or fail closed after submit uncertainty."""
    runtime = _load_runtime()
    work_dir = _executor_work_dir(control)
    executor = _executor(runtime, launch.executor, work_dir)
    intent_path = control.confined_path("psij_intent.json")
    if intent_path.exists():
        intent = read_intent(control)
        receipt_path = control.confined_path("psij_job.json")
        if not receipt_path.exists():
            raise _uncertain(control, executor=intent["executor"])
        receipt = read_receipt(control, intent)
        job = runtime.Job()
        executor.attach(job, receipt["native_id"])
        return PSIJLaunch(
            native_id=receipt["native_id"],
            executor_name=receipt["executor"],
            executor_work_dir=work_dir,
            job=job,
            executor=executor,
        )
    intent = write_intent(control, launch, work_dir)
    spec = _build_spec(runtime, intent["job"])
    job = runtime.Job(spec)
    try:
        executor.submit(job)
        native_id = validate_native_id(job.native_id)
        receipt = write_receipt(control, intent, native_id=native_id)
    except BaseException as error:
        raise _uncertain(
            control,
            executor=launch.executor,
            cause=error,
        ) from error
    state = _state_name(job.status.state)
    _append_observation(
        control,
        event=_event_for_state(state),
        executor=launch.executor,
        native_id=receipt["native_id"],
        state=state,
        message=None,
    )
    return PSIJLaunch(
        native_id=receipt["native_id"],
        executor_name=launch.executor,
        executor_work_dir=work_dir,
        job=job,
        executor=executor,
    )


def observe_psij(
    control: LauncherRunControl,
    *,
    timeout_seconds: float = 1.0,
) -> PSIJObservation:
    """Attach by durable native ID and return one bounded observation."""
    intent = read_intent(control)
    receipt = read_receipt(control, intent)
    runtime = _load_runtime()
    executor = _executor(
        runtime,
        receipt["executor"],
        Path(receipt["executor_work_dir"]),
    )
    job = runtime.Job()
    state: str | None = None
    try:
        executor.attach(job, receipt["native_id"])
        _verify_attached_id(job, receipt["native_id"])
        status = job.wait(
            timeout=timedelta(seconds=timeout_seconds),
            target_states=[
                runtime.JobState.QUEUED,
                runtime.JobState.ACTIVE,
                runtime.JobState.COMPLETED,
                runtime.JobState.FAILED,
                runtime.JobState.CANCELED,
            ],
        )
        state = _state_name(
            status.state if status is not None else job.status.state
        )
        _verify_attached_id(job, receipt["native_id"])
    except LauncherProtocolError:
        raise
    except Exception:
        state = None
    observation = PSIJObservation(
        executor=receipt["executor"],
        native_id=receipt["native_id"],
        state=state,
    )
    _append_observation(
        control,
        event=_event_for_state(state),
        executor=observation.executor,
        native_id=observation.native_id,
        state=observation.state,
        message=None,
    )
    return observation


def reconcile_psij(control: LauncherRunControl) -> PSIJObservation | None:
    """Reconcile scheduler metadata and fail an unclaimed terminal job."""
    intent_path = control.confined_path("psij_intent.json")
    if not intent_path.exists():
        return None
    receipt_path = control.confined_path("psij_job.json")
    if not receipt_path.exists():
        intent = read_intent(control)
        _append_observation(
            control,
            event="psij_submission_uncertain",
            executor=intent["executor"],
            native_id=None,
            state=None,
            message="PSI/J submission outcome is uncertain.",
        )
        return None
    observation = observe_psij(control)
    status = control.read_status()
    if status["state"] != "prepared" or not observation.final:
        return observation
    messages = {
        "COMPLETED": "The PSI/J orchestrator job completed before startup claim.",
        "FAILED": "The PSI/J orchestrator job failed before startup claim.",
        "CANCELED": "The PSI/J orchestrator job was cancelled before startup claim.",
    }
    terminal_state = observation.state
    assert terminal_state is not None
    error = LauncherProtocolError(
        messages[terminal_state],
        details={
            "run_id": control.run_id,
            "executor": observation.executor,
            "native_id": observation.native_id,
            "state": observation.state,
        },
    )
    try:
        control.commit_terminal(
            expected_revision=status["revision"],
            expected_claim_epoch=None,
            new_state="failed",
            error_payload=build_error_payload(
                control.run_id,
                code="psij-job-terminal-before-claim",
                error=error,
                backend={"name": "psij"},
            ),
        )
    except RevisionConflictError:
        pass
    return observation


def cancel_psij(control: LauncherRunControl) -> bool:
    """Best-effort cancel the exact receipt-backed PSI/J job."""
    intent_path = control.confined_path("psij_intent.json")
    receipt_path = control.confined_path("psij_job.json")
    if not intent_path.exists() or not receipt_path.exists():
        return False
    intent = read_intent(control)
    receipt = read_receipt(control, intent)
    runtime = _load_runtime()
    executor = _executor(
        runtime,
        receipt["executor"],
        Path(receipt["executor_work_dir"]),
    )
    job = runtime.Job()
    try:
        executor.attach(job, receipt["native_id"])
        _verify_attached_id(job, receipt["native_id"])
        job.cancel()
        status = job.wait(
            timeout=timedelta(seconds=1),
            target_states=[
                runtime.JobState.COMPLETED,
                runtime.JobState.FAILED,
                runtime.JobState.CANCELED,
            ],
        )
        _verify_attached_id(job, receipt["native_id"])
    except LauncherProtocolError:
        raise
    except Exception:
        return False
    state = _state_name(status.state if status is not None else job.status.state)
    _append_observation(
        control,
        event=_event_for_state(state),
        executor=receipt["executor"],
        native_id=receipt["native_id"],
        state=state,
        message=None,
    )
    return state in _TERMINAL_STATES
