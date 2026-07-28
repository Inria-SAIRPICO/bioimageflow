"""Detached submitted-workflow orchestrator entry point."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import traceback
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from bioimageflow.engine import WorkflowCancelledError
from bioimageflow.events import ProgressEvent
from bioimageflow.parsl import ExecutorBinding, ParslEngine, ParslTaskPolicy
from bioimageflow.storage import Storage
from bioimageflow.workflow import WorkflowExecutionContext

from .artifacts import build_error_payload, read_local_process_identity
from .configuration import build_parsl_config
from .errors import LauncherProtocolError
from .inputs import LoadedInvocation, load_invocation
from .orchestrator_monitor import (
    CancellationWatcher,
    ClaimHeartbeat,
    append_backend_event,
    owner_identity,
    public_progress_payload,
)
from .payload import load_workflow_payload
from .control import LauncherRunControl
from .repository import LauncherRepository
from .return_routes import build_return_provider_routes
from .returns import load_return_manifest, persist_public_return
from .state import ClaimEpochMismatchError, RevisionConflictError
from .types import OrchestratorLaunchConfig, ParslConfigRef


_DEFAULT_LEASE_SECONDS = 30.0
_DEFAULT_POLL_SECONDS = 0.1
_LOCAL_IDENTITY_WAIT_SECONDS = 10.0


@dataclass(frozen=True, slots=True)
class _PreparedExecution:
    workflow: Any
    invocation: LoadedInvocation
    engine: Any
    launch: OrchestratorLaunchConfig


class _StaleOrchestrator(RuntimeError):
    """Raised internally when a newer claim owns all further mutations."""


class _RedactingTextStream:
    """Replace known resolved secrets before Python text reaches launcher logs."""

    def __init__(self, stream: Any, redactions: Collection[str]) -> None:
        self._stream = stream
        self._redactions = tuple(
            value for value in redactions if isinstance(value, str) and value
        )

    def write(self, text: str) -> int:
        redacted = text
        for secret in self._redactions:
            redacted = redacted.replace(secret, "[REDACTED]")
        self._stream.write(redacted)
        return len(text)

    def flush(self) -> None:
        self._stream.flush()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._stream, name)


def _prepare_execution(
    control: LauncherRunControl,
    submission: Mapping[str, Any],
    *,
    trusted_factories: Collection[str] | None,
    claim_epoch: int,
    claim_nonce: str,
) -> _PreparedExecution:
    storage_root = Path(submission["storage_root"])
    workflow = load_workflow_payload(
        submission["workflow"],
        storage_path=storage_root,
    )
    invocation = load_invocation(
        workflow,
        submission["invocation"],
        control_dir=control.control_dir,
    )
    config_ref = ParslConfigRef.from_dict(submission["parsl_config"])
    config = build_parsl_config(
        config_ref,
        trusted_factories=trusted_factories,
    )
    raw_bindings = submission["executor_bindings"]
    if not isinstance(raw_bindings, Mapping):
        raise LauncherProtocolError("Submitted executor bindings are invalid.")
    bindings = {
        label: ExecutorBinding.from_dict(binding)
        for label, binding in raw_bindings.items()
    }
    task_policy = ParslTaskPolicy.from_dict(submission["task_policy"])
    launch = OrchestratorLaunchConfig.from_dict(submission["launch"])

    def on_progress(event: ProgressEvent) -> None:
        control.append_progress(
            kind="public",
            payload=public_progress_payload(event),
            expected_claim_epoch=claim_epoch,
            expected_claim_nonce=claim_nonce,
        )

    workflow.on_progress = on_progress
    engine = ParslEngine(
        parsl_config=config,
        executor_bindings=bindings,
        node_routes=submission["node_routes"],
        environment_routes=submission["environment_routes"],
        shared_runtime_root=submission["shared_runtime_root"],
        execution="workflow",
        task_policy=task_policy,
        resource_lifetime="execution",
    )
    return _PreparedExecution(
        workflow=workflow,
        invocation=invocation,
        engine=engine,
        launch=launch,
    )


def _execute_workflow(
    prepared: _PreparedExecution,
    context: WorkflowExecutionContext,
) -> Any:
    workflow = prepared.workflow
    invocation = prepared.invocation
    if invocation.variant == "root":
        return workflow.compute(
            inputs=invocation.inputs,
            engine=prepared.engine,
            run_context=context,
        )
    nodes = workflow.nodes
    targets = tuple(nodes[name] for name in invocation.targets)
    return workflow.compute(
        *targets,
        engine=prepared.engine,
        run_context=context,
    )


def _transition_or_observe_cancel(
    control: LauncherRunControl,
    *,
    expected_revision: int,
    claim_epoch: int,
    new_state: str,
    context: WorkflowExecutionContext,
    workflow: Any,
) -> dict[str, Any] | None:
    try:
        return control.transition(
            expected_revision=expected_revision,
            expected_claim_epoch=claim_epoch,
            new_state=new_state,
        )
    except (RevisionConflictError, ClaimEpochMismatchError):
        current = control.read_status()
        if current["state"] in {"cancel_requested", "cancelled"}:
            context.request_cancel()
            workflow.cancel()
            return None
        if current["claim_epoch"] != claim_epoch:
            raise _StaleOrchestrator(
                "A newer recovery claim owns the submitted run."
            ) from None
        raise


def _observe_durable_cancellation(
    control: LauncherRunControl,
    *,
    context: WorkflowExecutionContext,
    workflow: Any,
) -> bool:
    status = control.read_status()
    if status["state"] not in {"cancel_requested", "cancelled"}:
        return False
    context.request_cancel()
    workflow.cancel()
    return True


def _task_error_details(
    error: BaseException,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None, str | None]:
    node_name = getattr(error, "scoped_node_name", None)
    task_id = getattr(error, "task_id", None)
    if not isinstance(node_name, str):
        node_name = getattr(error, "node_name", None)
    node = {"name": node_name} if isinstance(node_name, str) and node_name else None
    task: dict[str, Any] | None = None
    if isinstance(task_id, str) and task_id:
        task = {
            "task_id": task_id,
            "invocation_id": getattr(error, "invocation_id", None),
            "cache_attempt_id": getattr(error, "cache_attempt_id", None),
            "row_position": getattr(error, "row_position", None),
        }
    remote = getattr(error, "remote_traceback", None)
    return node, task, remote if isinstance(remote, str) else None


def _secret_redactions(submission: Mapping[str, Any]) -> tuple[str, ...]:
    config = submission.get("parsl_config")
    if not isinstance(config, Mapping):
        return ()
    references = config.get("secret_refs")
    if not isinstance(references, Mapping):
        return ()
    values: list[str] = []
    for reference in references.values():
        if isinstance(reference, str):
            value = os.environ.get(reference)
            if value:
                values.append(value)
    return tuple(values)


def _terminal_error_payload(
    control: LauncherRunControl,
    *,
    code: str,
    error: BaseException,
    redactions: Collection[str] = (),
) -> dict[str, Any]:
    node, task, remote_traceback = _task_error_details(error)
    return build_error_payload(
        control.run_id,
        code=code,
        error=error,
        traceback_text=remote_traceback,
        node=node,
        task=task,
        backend={"name": control.read_status()["backend"]},
        redactions=tuple(redactions),
    )


def _canonical_run_exists(storage_root: Path, run_id: str) -> bool:
    path = storage_root / "views" / "runs" / run_id / "run.json"
    return path.is_file() and not path.is_symlink()


def _canonical_run_status(storage_root: Path, run_id: str) -> str | None:
    path = storage_root / "views" / "runs" / run_id / "run.json"
    if path.is_symlink() or not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    status = payload.get("status") if isinstance(payload, dict) else None
    return status if isinstance(status, str) else None


def _finalize_canonical_failure(
    storage_root: Path,
    run_id: str,
    error: BaseException,
) -> None:
    if not _canonical_run_exists(storage_root, run_id):
        return
    context_status = (
        "cancelled"
        if isinstance(error, WorkflowCancelledError)
        else "failed"
    )
    Storage(storage_root).finalize_run_metadata(
        run_id,
        status=context_status,
        update_latest_success=False,
    )


def _commit_terminal(
    control: LauncherRunControl,
    *,
    state: str,
    claim_epoch: int,
    error_payload: Mapping[str, Any] | None = None,
    updates: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    status = control.read_status()
    if status["state"] == state:
        return status
    if status["state"] in {"succeeded", "failed", "cancelled", "lost"}:
        return status
    return control.commit_terminal(
        expected_revision=status["revision"],
        expected_claim_epoch=claim_epoch,
        new_state=state,
        error_payload=error_payload,
        updates=updates,
    )


def _recover_post_start(
    control: LauncherRunControl,
    *,
    owner: str,
    backend_absent_confirmed: bool,
    lease_seconds: float,
) -> str:
    status = control.read_status()
    if status["state"] in {"succeeded", "failed", "cancelled", "lost"}:
        return str(status["state"])
    if status["claim_epoch"] is None:
        raise LauncherProtocolError("Post-start recovery requires a claim epoch.")
    takeover = control.takeover_claim(
        expected_revision=status["revision"],
        expected_claim_epoch=status["claim_epoch"],
        owner=owner,
        backend=status["backend"],
        lease_seconds=lease_seconds,
        backend_absent_confirmed=backend_absent_confirmed,
    )
    status = takeover.status
    storage_root = control.repository.storage_root
    if (
        status["state"] == "cancel_requested"
        and _canonical_run_status(storage_root, control.run_id) == "cancelled"
    ):
        terminal = _commit_terminal(
            control,
            state="cancelled",
            claim_epoch=takeover.claim["epoch"],
        )
        return str(terminal["state"])
    if status["state"] == "finalizing":
        try:
            load_return_manifest(
                control.control_dir,
                expected_run_id=control.run_id,
            )
            Storage(storage_root).finalize_run_metadata(
                control.run_id,
                status="succeeded",
                update_latest_success=True,
            )
        except BaseException as exc:
            if _canonical_run_status(storage_root, control.run_id) == "succeeded":
                raise LauncherProtocolError(
                    "Canonical success is durable but its latest-success index "
                    "has not converged.",
                    details={"run_id": control.run_id},
                ) from exc
        else:
            control.transition(
                expected_revision=status["revision"],
                expected_claim_epoch=takeover.claim["epoch"],
                new_state="succeeded",
            )
            return "succeeded"

    lost = LauncherProtocolError(
        "The prior orchestrator disappeared after workflow startup.",
        details={"run_id": control.run_id, "state": status["state"]},
    )
    _finalize_canonical_failure(storage_root, control.run_id, lost)
    error_payload = _terminal_error_payload(
        control,
        code="orchestrator-lost",
        error=lost,
    )
    terminal = _commit_terminal(
        control,
        state="lost",
        claim_epoch=takeover.claim["epoch"],
        error_payload=error_payload,
    )
    return str(terminal["state"])


def _await_local_process_identity(control: LauncherRunControl) -> None:
    """Do not claim a local launch until its parent has installed tracking."""
    identity_path = control.confined_path("local_process.json")
    deadline = time.monotonic() + _LOCAL_IDENTITY_WAIT_SECONDS
    while not identity_path.exists():
        status = control.read_status()
        if status["state"] != "prepared":
            raise LauncherProtocolError(
                "Local launch left prepared state before identity installation.",
                details={"run_id": control.run_id, "state": status["state"]},
            )
        if time.monotonic() >= deadline:
            raise LauncherProtocolError(
                "Local process identity was not installed before startup.",
                details={"run_id": control.run_id},
            )
        time.sleep(0.01)
    identity = read_local_process_identity(control)
    if identity["pid"] != os.getpid():
        raise LauncherProtocolError(
            "Local process identity does not match the orchestrator process.",
            details={"run_id": control.run_id},
        )


def run_orchestrator(
    storage_root: str | Path,
    run_id: str,
    *,
    trusted_factories: Collection[str] | None = None,
    lease_seconds: float = _DEFAULT_LEASE_SECONDS,
    poll_seconds: float = _DEFAULT_POLL_SECONDS,
    recover: bool = False,
    backend_absent_confirmed: bool = False,
) -> str:
    """Execute or recover one already allocated submitted run."""
    if lease_seconds <= 0 or poll_seconds <= 0:
        raise ValueError("Lease and poll intervals must be positive.")
    control = LauncherRepository(storage_root).open(run_id)
    submission = control.read_submission()
    if submission["launch"]["backend"] == "local" and not recover:
        _await_local_process_identity(control)
    status = control.read_status()
    owner = owner_identity()

    if status["state"] != "prepared":
        if not recover:
            raise LauncherProtocolError(
                "An orchestrator may execute only a prepared run.",
                details={"run_id": run_id, "state": status["state"]},
            )
        return _recover_post_start(
            control,
            owner=owner,
            backend_absent_confirmed=backend_absent_confirmed,
            lease_seconds=lease_seconds,
        )

    claimed = control.claim_start(
        expected_revision=status["revision"],
        owner=owner,
        backend=status["backend"],
        lease_seconds=lease_seconds,
        backend_absent_confirmed=backend_absent_confirmed,
    )
    claim = claimed.claim
    claim_epoch = claim["epoch"]
    heartbeat = ClaimHeartbeat(
        control,
        claim=claim,
        workflow=None,
        lease_seconds=lease_seconds,
        poll_seconds=poll_seconds,
    )
    heartbeat.start()
    prepared: _PreparedExecution | None = None
    context = WorkflowExecutionContext(
        run_id=run_id,
        defer_success_finalization=True,
    )
    context._authorize_launcher_reservation(submission["storage_root"])
    redactions = _secret_redactions(submission)
    watcher: CancellationWatcher | None = None
    durable_result = "failed"
    try:
        append_backend_event(
            control,
            "orchestrator_starting",
            owner=owner,
            claim_epoch=claim_epoch,
            claim_nonce=claim["nonce"],
        )
        prepared = _prepare_execution(
            control,
            submission,
            trusted_factories=trusted_factories,
            claim_epoch=claim_epoch,
            claim_nonce=claim["nonce"],
        )
        heartbeat.attach_workflow(prepared.workflow)
        status = control.read_status()
        if status["state"] == "cancel_requested":
            context.request_cancel()
            prepared.workflow.cancel()
            _commit_terminal(
                control,
                state="cancelled",
                claim_epoch=claim_epoch,
            )
            return "cancelled"
        status = _transition_or_observe_cancel(
            control,
            expected_revision=status["revision"],
            claim_epoch=claim_epoch,
            new_state="running",
            context=context,
            workflow=prepared.workflow,
        )
        if status is None:
            _commit_terminal(
                control,
                state="cancelled",
                claim_epoch=claim_epoch,
            )
            return "cancelled"
        append_backend_event(
            control,
            "orchestrator_running",
            owner=owner,
            claim_epoch=claim_epoch,
            claim_nonce=claim["nonce"],
        )
        watcher = CancellationWatcher(
            control,
            prepared.workflow,
            context,
            poll_seconds=poll_seconds,
        )
        watcher.start()
        if _observe_durable_cancellation(
            control,
            context=context,
            workflow=prepared.workflow,
        ):
            raise WorkflowCancelledError(
                "Cancellation won before submitted workflow execution."
            )
        result = _execute_workflow(prepared, context)
        watcher.raise_if_failed()
        heartbeat.raise_if_failed()
        outcomes = context.execution_outcomes
        persist_public_return(
            control.control_dir,
            Path(submission["storage_root"]),
            run_id,
            result,
            outcomes=outcomes,
            root_outputs=prepared.invocation.outputs,
            provider_routes=build_return_provider_routes(
                prepared.workflow,
                prepared.invocation,
                outcomes,
            ),
        )
        status = control.read_status()
        if status["state"] == "cancel_requested":
            cancellation = WorkflowCancelledError(
                "Cancellation won before submitted success finalization."
            )
            context.finalize_failure(cancellation)
            _commit_terminal(
                control,
                state="cancelled",
                claim_epoch=claim_epoch,
            )
            return "cancelled"
        status = _transition_or_observe_cancel(
            control,
            expected_revision=status["revision"],
            claim_epoch=claim_epoch,
            new_state="finalizing",
            context=context,
            workflow=prepared.workflow,
        )
        if status is None:
            cancellation = WorkflowCancelledError(
                "Cancellation won before submitted success finalization."
            )
            context.finalize_failure(cancellation)
            _commit_terminal(
                control,
                state="cancelled",
                claim_epoch=claim_epoch,
            )
            return "cancelled"
        try:
            context.finalize_success()
        except BaseException:
            if (
                _canonical_run_status(
                    Path(submission["storage_root"]),
                    run_id,
                )
                == "succeeded"
            ):
                return "finalizing"
            raise
        control.commit_terminal(
            expected_revision=status["revision"],
            expected_claim_epoch=claim_epoch,
            new_state="succeeded",
        )
        durable_result = "succeeded"
        try:
            append_backend_event(
                control,
                "orchestrator_succeeded",
                owner=owner,
                claim_epoch=claim_epoch,
                claim_nonce=claim["nonce"],
                allow_terminal_claim=True,
            )
        except BaseException:
            pass
        return "succeeded"
    except _StaleOrchestrator:
        return str(control.read_status()["state"])
    except WorkflowCancelledError as exc:
        if context.terminal_status is None:
            try:
                context.finalize_failure(exc)
            except RuntimeError:
                _finalize_canonical_failure(
                    Path(submission["storage_root"]),
                    run_id,
                    exc,
                )
        status = control.read_status()
        if status["state"] not in {"cancel_requested", "cancelled"}:
            error_payload = _terminal_error_payload(
                control,
                code="workflow-cancelled",
                error=exc,
                redactions=redactions,
            )
            _commit_terminal(
                control,
                state="failed",
                claim_epoch=claim_epoch,
                error_payload=error_payload,
            )
            return "failed"
        _commit_terminal(
            control,
            state="cancelled",
            claim_epoch=claim_epoch,
        )
        return "cancelled"
    except BaseException as exc:
        status = control.read_status()
        if status["state"] in {"cancel_requested", "cancelled"}:
            cancellation = WorkflowCancelledError(
                "Cancellation won during submitted workflow execution."
            )
            if context.terminal_status is None:
                try:
                    context.finalize_failure(cancellation)
                except RuntimeError:
                    _finalize_canonical_failure(
                        Path(submission["storage_root"]),
                        run_id,
                        cancellation,
                    )
            _commit_terminal(
                control,
                state="cancelled",
                claim_epoch=claim_epoch,
            )
            return "cancelled"
        if status["claim_epoch"] != claim_epoch:
            return str(status["state"])
        if context.terminal_status is None:
            try:
                context.finalize_failure(exc)
            except BaseException:
                if (
                    _canonical_run_status(
                        Path(submission["storage_root"]),
                        run_id,
                    )
                    != "succeeded"
                ):
                    _finalize_canonical_failure(
                        Path(submission["storage_root"]),
                        run_id,
                        exc,
                    )
        error_payload = _terminal_error_payload(
            control,
            code="workflow-execution-failed",
            error=exc,
            redactions=redactions,
        )
        _commit_terminal(
            control,
            state="failed",
            claim_epoch=claim_epoch,
            error_payload=error_payload,
        )
        return "failed"
    finally:
        if watcher is not None:
            watcher.stop()
        heartbeat.stop()
        if prepared is not None:
            close = getattr(prepared.engine, "close", None)
            if callable(close):
                try:
                    close()
                except BaseException:
                    if durable_result not in {
                        "succeeded",
                        "failed",
                        "cancelled",
                        "lost",
                    }:
                        raise


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run one allocated BioImageFlow submitted workflow.",
    )
    parser.add_argument("--storage-root", required=True, type=Path)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--recover", action="store_true")
    parser.add_argument("--backend-absent-confirmed", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the detached orchestrator command."""
    arguments = _parser().parse_args(argv)
    try:
        submission = LauncherRepository(arguments.storage_root).open(
            arguments.run_id
        ).read_submission()
        redactions = _secret_redactions(submission)
    except BaseException:
        redactions = ()
    if redactions:
        sys.stdout = _RedactingTextStream(sys.stdout, redactions)
        sys.stderr = _RedactingTextStream(sys.stderr, redactions)
    try:
        state = run_orchestrator(
            arguments.storage_root,
            arguments.run_id,
            recover=arguments.recover,
            backend_absent_confirmed=arguments.backend_absent_confirmed,
        )
    except BaseException:
        traceback.print_exc()
        return 1
    return 0 if state in {"succeeded", "cancelled"} else 1


if __name__ == "__main__":
    sys.exit(main())
