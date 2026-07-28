from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from bioimageflow import (
    ExecutorBinding,
    ExecutorCapabilities,
    WorkerEnvironmentAttestation,
    WorkerSlotCapacity,
    Workflow,
)
from bioimageflow.engine import WorkflowCancelledError
from bioimageflow.launcher.inputs import LoadedInvocation
from bioimageflow.launcher.orchestrator import _PreparedExecution, run_orchestrator
from bioimageflow.launcher.orchestrator_monitor import CancellationWatcher
from bioimageflow.launcher.control import LauncherRunControl
from bioimageflow.launcher.repository import LauncherCorruptionError
from bioimageflow.launcher.submission import submit_workflow
from bioimageflow.launcher.types import (
    OrchestratorLaunchConfig,
    ParslConfigRef,
)
from bioimageflow.storage import Storage
from bioimageflow.workflow import WorkflowExecutionContext


def _binding() -> ExecutorBinding:
    return ExecutorBinding(
        label="threads",
        environments=(
            WorkerEnvironmentAttestation(
                name="default",
                dependency_hash="0" * 64,
                allow_flexible_versions=False,
                core_requirement="bioimageflow-core==0.1.7",
            ),
        ),
        capabilities=ExecutorCapabilities(
            storage_modes=("shared_fs",),
            tool_origin_modes=("installed_module",),
            slot=WorkerSlotCapacity(cpu=1),
        ),
    )


def _submit_manual(storage_path: Path):
    return submit_workflow(
        Workflow(storage_path=storage_path, engine="direct"),
        parsl_config=ParslConfigRef(
            "tests.unit.launcher.config_factories:build",
            {"workers": 1},
        ),
        executor_bindings={"threads": _binding()},
        launch=OrchestratorLaunchConfig(backend="manual"),
    )


def test_manual_orchestrator_executes_and_reconnects_zero_output_run(
    tmp_path: Path,
) -> None:
    run = _submit_manual(tmp_path)

    terminal = run_orchestrator(
        tmp_path,
        run.id,
        lease_seconds=2,
        poll_seconds=0.01,
    )

    run.refresh()
    result = run.result()
    assert terminal == "succeeded"
    assert run.status == "succeeded"
    assert isinstance(result, pd.DataFrame)
    assert result.empty
    assert run.view_dir == tmp_path / "views" / "runs" / run.id
    assert Storage(tmp_path).latest_success_run_id() == run.id
    assert [entry["sequence"] for entry in run.progress()] == list(
        range(1, len(run.progress()) + 1)
    )


def test_invalid_invocation_is_rejected_before_canonical_workflow_start(
    tmp_path: Path,
) -> None:
    run = _submit_manual(tmp_path)
    submission_path = run.control_dir / "submission.json"
    submission = run._control.read_submission()
    submission["invocation"] = {
        "schema": "unsupported",
        "variant": "root",
    }
    submission_path.write_text(json.dumps(submission, sort_keys=True))

    with pytest.raises(
        LauncherCorruptionError,
        match="Invalid launcher submission",
    ):
        run_orchestrator(
            tmp_path,
            run.id,
            lease_seconds=2,
            poll_seconds=0.01,
        )

    run.refresh()
    assert run.status == "prepared"
    assert not run.view_dir.exists()
    assert not (run.control_dir / "error.json").exists()


def test_configuration_failure_never_persists_secret_value(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "literal-orchestrator-secret"
    monkeypatch.setenv("BIF_TEST_CREDENTIAL", secret)
    run = submit_workflow(
        Workflow(storage_path=tmp_path, engine="direct"),
        parsl_config=ParslConfigRef(
            "tests.unit.launcher.config_factories:fail_with_credential",
            {},
            secret_refs={"credential": "BIF_TEST_CREDENTIAL"},
        ),
        executor_bindings={"threads": _binding()},
        launch=OrchestratorLaunchConfig(backend="manual"),
    )

    terminal = run_orchestrator(
        tmp_path,
        run.id,
        lease_seconds=2,
        poll_seconds=0.01,
    )

    assert terminal == "failed"
    assert secret not in (run.control_dir / "error.json").read_text()


def test_cancellation_marker_reaches_active_workflow(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = _submit_manual(tmp_path)
    started = threading.Event()

    class CancellableWorkflow:
        def __init__(self) -> None:
            self.cancel_requested = False

        def cancel(self) -> None:
            self.cancel_requested = True

    def fake_prepare(*args: Any, **kwargs: Any) -> _PreparedExecution:
        workflow = CancellableWorkflow()
        return _PreparedExecution(
            workflow=workflow,
            invocation=LoadedInvocation(
                variant="root",
                inputs={},
                targets=(),
                outputs=(),
            ),
            engine=object(),
            launch=OrchestratorLaunchConfig(backend="manual"),
        )

    def wait_for_cancel(
        prepared: _PreparedExecution,
        context: WorkflowExecutionContext,
    ) -> Any:
        context._bind(
            object(),
            on_success=lambda: None,
            on_failure=lambda error: None,
        )
        started.set()
        while not prepared.workflow.cancel_requested:
            time.sleep(0.005)
        error = WorkflowCancelledError("cancelled")
        context._execution_failed(error)
        raise error

    monkeypatch.setattr(
        "bioimageflow.launcher.orchestrator._prepare_execution",
        fake_prepare,
    )
    monkeypatch.setattr(
        "bioimageflow.launcher.orchestrator._execute_workflow",
        wait_for_cancel,
    )
    result: list[str] = []
    thread = threading.Thread(
        target=lambda: result.append(
            run_orchestrator(
                tmp_path,
                run.id,
                lease_seconds=2,
                poll_seconds=0.01,
            )
        ),
        daemon=True,
    )
    thread.start()
    assert started.wait(2)

    run.cancel()
    thread.join(2)
    run.refresh()

    assert result == ["cancelled"]
    assert run.status == "cancelled"
    assert (run.control_dir / "cancel_requested").is_file()


def test_cancellation_marker_without_status_does_not_cancel(
    tmp_path: Path,
) -> None:
    run = _submit_manual(tmp_path)
    claimed = run._control.claim_start(
        expected_revision=0,
        owner="marker-test",
        backend="manual",
        lease_seconds=30,
    )
    run._control.transition(
        expected_revision=claimed.status["revision"],
        expected_claim_epoch=claimed.claim["epoch"],
        new_state="running",
    )
    (run.control_dir / "cancel_requested").touch()

    class WorkflowProbe:
        cancelled = False

        def cancel(self) -> None:
            self.cancelled = True

    workflow = WorkflowProbe()
    context = WorkflowExecutionContext(run.id)
    watcher = CancellationWatcher(
        run._control,
        workflow,
        context,
        poll_seconds=0.01,
    )
    watcher.start()
    watcher.stop()

    assert workflow.cancelled is False
    assert context.cancel_requested is False


@pytest.mark.parametrize("raced_transition", ["running", "finalizing"])
def test_cancellation_cas_race_remains_cancelled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    raced_transition: str,
) -> None:
    run = _submit_manual(tmp_path)
    original = LauncherRunControl.transition
    raced = False

    def racing_transition(
        control: LauncherRunControl,
        **kwargs: Any,
    ) -> dict[str, Any]:
        nonlocal raced
        if (
            control.run_id == run.id
            and kwargs.get("new_state") == raced_transition
            and not raced
        ):
            raced = True
            run.cancel()
        return original(control, **kwargs)

    monkeypatch.setattr(
        LauncherRunControl,
        "transition",
        racing_transition,
    )

    terminal = run_orchestrator(
        tmp_path,
        run.id,
        lease_seconds=2,
        poll_seconds=0.01,
    )

    run.refresh()
    assert raced is True
    assert terminal == "cancelled"
    assert run.status == "cancelled"
    assert not (run.control_dir / "error.json").exists()


def test_cancel_wins_after_return_install_but_before_success_claim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = _submit_manual(tmp_path)
    installed = threading.Event()
    release = threading.Event()
    from bioimageflow.launcher import orchestrator as module

    real_persist = module.persist_public_return

    def blocking_persist(*args: Any, **kwargs: Any) -> dict[str, Any]:
        manifest = real_persist(*args, **kwargs)
        installed.set()
        assert release.wait(2)
        return manifest

    monkeypatch.setattr(module, "persist_public_return", blocking_persist)
    result: list[str] = []
    thread = threading.Thread(
        target=lambda: result.append(
            run_orchestrator(
                tmp_path,
                run.id,
                lease_seconds=2,
                poll_seconds=0.01,
            )
        )
    )
    thread.start()
    assert installed.wait(2)

    run.cancel()
    release.set()
    thread.join(2)
    run.refresh()

    assert result == ["cancelled"]
    assert run.status == "cancelled"
    assert (run.control_dir / "return" / "manifest.json").is_file()
    assert Storage(tmp_path).latest_success_run_id() is None
