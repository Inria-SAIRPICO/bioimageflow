"""Detached submitted-workflow orchestrator entry point."""

from __future__ import annotations

import argparse
import os
import socket
import sys
import threading
import traceback
import uuid
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from bioimageflow.engine import WorkflowCancelledError
from bioimageflow.events import ProgressEvent
from bioimageflow.parsl import ExecutorBinding, ParslEngine, ParslTaskPolicy
from bioimageflow.storage import Storage
from bioimageflow.workflow import WorkflowExecutionContext

from .artifacts import write_error
from .configuration import build_parsl_config
from .errors import LauncherProtocolError
from .inputs import LoadedInvocation, load_invocation
from .payload import load_workflow_payload
from .repository import LauncherRepository, LauncherRunControl
from .returns import load_return_manifest, persist_public_return
from .types import OrchestratorLaunchConfig, ParslConfigRef


_DEFAULT_LEASE_SECONDS = 30.0
_DEFAULT_POLL_SECONDS = 0.1
_PUBLIC_PROGRESS_SCHEMA = "bioimageflow.progress_event.v1"
_BACKEND_PROGRESS_SCHEMA = "bioimageflow.launcher.backend_event.v1"


@dataclass(frozen=True, slots=True)
class _PreparedExecution:
    workflow: Any
    invocation: LoadedInvocation
    engine: Any
    launch: OrchestratorLaunchConfig


class _ClaimHeartbeat:
    def __init__(
        self,
        control: LauncherRunControl,
        *,
        claim: Mapping[str, Any],
        workflow: Any | None,
        lease_seconds: float,
        poll_seconds: float,
    ) -> None:
        self._control = control
        self._claim = dict(claim)
        self._workflow = workflow
        self._lease_seconds = lease_seconds
        self._interval = min(poll_seconds, lease_seconds / 3)
        self._stop = threading.Event()
        self._failure: BaseException | None = None
        self._thread = threading.Thread(
            target=self._run,
            name=f"bioimageflow-heartbeat-{control.run_id}",
            daemon=True,
        )

    def start(self) -> None:
        self._thread.start()

    def attach_workflow(self, workflow: Any) -> None:
        self._workflow = workflow

    def stop(self) -> None:
        self._stop.set()
        self._thread.join()

    def raise_if_failed(self) -> None:
        if self._failure is not None:
            raise LauncherProtocolError(
                "The orchestrator lost its execution claim.",
                details={"run_id": self._control.run_id},
            ) from self._failure

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                self._claim = self._control.heartbeat_claim(
                    expected_epoch=self._claim["epoch"],
                    expected_nonce=self._claim["nonce"],
                    lease_seconds=self._lease_seconds,
                )
            except BaseException as exc:
                self._failure = exc
                if self._workflow is not None:
                    self._workflow.cancel()
                return


class _CancellationWatcher:
    def __init__(
        self,
        control: LauncherRunControl,
        workflow: Any,
        *,
        poll_seconds: float,
    ) -> None:
        self._control = control
        self._workflow = workflow
        self._poll_seconds = poll_seconds
        self._stop = threading.Event()
        self._failure: BaseException | None = None
        self._thread = threading.Thread(
            target=self._run,
            name=f"bioimageflow-cancel-{control.run_id}",
            daemon=True,
        )

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join()

    def raise_if_failed(self) -> None:
        if self._failure is not None:
            raise LauncherProtocolError(
                "The orchestrator cancellation watcher failed.",
                details={"run_id": self._control.run_id},
            ) from self._failure

    def _run(self) -> None:
        while not self._stop.wait(self._poll_seconds):
            try:
                status = self._control.read_status()
                marker = self._control.cancellation_marker_exists()
            except BaseException as exc:
                self._failure = exc
                self._workflow.cancel()
                return
            if status["state"] == "cancel_requested" or marker:
                self._workflow.cancel()
            if status["state"] in {"finalizing", "succeeded", "failed", "cancelled", "lost"}:
                return


def _owner_identity() -> str:
    return (
        f"{socket.gethostname()}:{os.getpid()}:"
        f"{uuid.uuid4().hex}"
    )


def _public_progress_payload(event: ProgressEvent) -> dict[str, Any]:
    return {
        "schema": _PUBLIC_PROGRESS_SCHEMA,
        "node_name": event.node_name,
        "status": event.status,
        "row": event.row,
        "total_rows": event.total_rows,
        "message": event.message,
        "current": event.current,
        "maximum": event.maximum,
        "timestamp": event.timestamp,
        "result_key": event.result_key,
        "record_id": event.record_id,
    }


def _append_backend_event(
    control: LauncherRunControl,
    event: str,
    **details: Any,
) -> None:
    control.append_progress(
        kind="backend",
        payload={
            "schema": _BACKEND_PROGRESS_SCHEMA,
            "event": event,
            **details,
        },
    )


def _prepare_execution(
    control: LauncherRunControl,
    submission: Mapping[str, Any],
    *,
    trusted_factories: Collection[str] | None,
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
            payload=_public_progress_payload(event),
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


def _write_terminal_error(
    control: LauncherRunControl,
    *,
    code: str,
    error: BaseException,
) -> None:
    node, task, remote_traceback = _task_error_details(error)
    write_error(
        control,
        code=code,
        error=error,
        traceback_text=remote_traceback,
        node=node,
        task=task,
        backend={"name": control.read_status()["backend"]},
    )


def _canonical_run_exists(storage_root: Path, run_id: str) -> bool:
    path = storage_root / "views" / "runs" / run_id / "run.json"
    return path.is_file() and not path.is_symlink()


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


def _transition_terminal(
    control: LauncherRunControl,
    *,
    state: str,
    claim_epoch: int,
    error_path: str | None,
) -> dict[str, Any]:
    status = control.read_status()
    if status["state"] == state:
        return status
    if status["state"] in {"succeeded", "failed", "cancelled", "lost"}:
        return status
    return control.transition(
        expected_revision=status["revision"],
        expected_claim_epoch=claim_epoch,
        new_state=state,
        updates={"error": error_path},
    )


def _recover_post_start(
    control: LauncherRunControl,
    *,
    owner: str,
    backend_absent_confirmed: bool,
    lease_seconds: float,
) -> str:
    status = control.read_status()
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
        except BaseException:
            pass
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
    _write_terminal_error(
        control,
        code="orchestrator-lost",
        error=lost,
    )
    control.transition(
        expected_revision=status["revision"],
        expected_claim_epoch=takeover.claim["epoch"],
        new_state="lost",
        updates={"error": "error.json"},
    )
    return "lost"


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
    status = control.read_status()
    owner = _owner_identity()

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
    heartbeat = _ClaimHeartbeat(
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
    watcher: _CancellationWatcher | None = None
    try:
        _append_backend_event(control, "orchestrator_starting", owner=owner)
        prepared = _prepare_execution(
            control,
            submission,
            trusted_factories=trusted_factories,
        )
        heartbeat.attach_workflow(prepared.workflow)
        status = control.read_status()
        if status["state"] == "cancel_requested":
            prepared.workflow.cancel()
            control.transition(
                expected_revision=status["revision"],
                expected_claim_epoch=claim_epoch,
                new_state="cancelled",
            )
            return "cancelled"
        status = control.transition(
            expected_revision=status["revision"],
            expected_claim_epoch=claim_epoch,
            new_state="running",
        )
        _append_backend_event(control, "orchestrator_running", owner=owner)
        watcher = _CancellationWatcher(
            control,
            prepared.workflow,
            poll_seconds=poll_seconds,
        )
        watcher.start()
        result = _execute_workflow(prepared, context)
        watcher.raise_if_failed()
        heartbeat.raise_if_failed()
        persist_public_return(
            control.control_dir,
            Path(submission["storage_root"]),
            run_id,
            result,
            outcomes=context.execution_outcomes,
            root_outputs=prepared.invocation.outputs,
        )
        status = control.read_status()
        if status["state"] == "cancel_requested":
            cancellation = WorkflowCancelledError(
                "Cancellation won before submitted success finalization."
            )
            context.finalize_failure(cancellation)
            control.transition(
                expected_revision=status["revision"],
                expected_claim_epoch=claim_epoch,
                new_state="cancelled",
            )
            return "cancelled"
        status = control.transition(
            expected_revision=status["revision"],
            expected_claim_epoch=claim_epoch,
            new_state="finalizing",
        )
        context.finalize_success()
        control.transition(
            expected_revision=status["revision"],
            expected_claim_epoch=claim_epoch,
            new_state="succeeded",
        )
        _append_backend_event(control, "orchestrator_succeeded", owner=owner)
        return "succeeded"
    except WorkflowCancelledError as exc:
        if context.terminal_status is None:
            _finalize_canonical_failure(Path(submission["storage_root"]), run_id, exc)
        status = control.read_status()
        if status["state"] not in {"cancel_requested", "cancelled"}:
            _write_terminal_error(
                control,
                code="workflow-cancelled",
                error=exc,
            )
            _transition_terminal(
                control,
                state="failed",
                claim_epoch=claim_epoch,
                error_path="error.json",
            )
            return "failed"
        _transition_terminal(
            control,
            state="cancelled",
            claim_epoch=claim_epoch,
            error_path=None,
        )
        return "cancelled"
    except BaseException as exc:
        if context.terminal_status is None:
            try:
                context.finalize_failure(exc)
            except RuntimeError:
                _finalize_canonical_failure(
                    Path(submission["storage_root"]),
                    run_id,
                    exc,
                )
        try:
            _write_terminal_error(
                control,
                code="workflow-execution-failed",
                error=exc,
            )
        except BaseException:
            pass
        _transition_terminal(
            control,
            state="failed",
            claim_epoch=claim_epoch,
            error_path="error.json",
        )
        return "failed"
    finally:
        if watcher is not None:
            watcher.stop()
        heartbeat.stop()
        if prepared is not None:
            close = getattr(prepared.engine, "close", None)
            if callable(close):
                close()


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
