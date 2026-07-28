from __future__ import annotations

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
from bioimageflow.launcher.orchestrator import run_orchestrator
from bioimageflow.launcher.returns import persist_public_return
from bioimageflow.launcher.submission import submit_workflow
from bioimageflow.launcher.types import (
    OrchestratorLaunchConfig,
    ParslConfigRef,
)
from bioimageflow.storage import Storage


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


def _expired_post_start(
    tmp_path: Path,
    *,
    finalizing: bool,
) -> Any:
    run = _submit_manual(tmp_path)
    control = run._control
    status = control.read_status()
    claimed = control.claim_start(
        expected_revision=status["revision"],
        owner="dead-owner",
        backend="manual",
        lease_seconds=0.01,
    )
    status = control.transition(
        expected_revision=claimed.status["revision"],
        expected_claim_epoch=claimed.claim["epoch"],
        new_state="running",
    )
    if finalizing:
        Storage(tmp_path).write_run_metadata(
            run.id,
            workflow_identity="workflow:test",
            engine="parsl:parallel",
            status="running",
            target_nodes=[],
        )
        persist_public_return(
            control.control_dir,
            tmp_path,
            run.id,
            pd.DataFrame(),
            outcomes=(),
        )
        control.transition(
            expected_revision=status["revision"],
            expected_claim_epoch=claimed.claim["epoch"],
            new_state="finalizing",
        )
    time.sleep(0.02)
    return run


def test_recovery_completes_installed_finalization_without_rerun(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = _expired_post_start(tmp_path, finalizing=True)
    monkeypatch.setattr(
        "bioimageflow.launcher.orchestrator._prepare_execution",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("recovery must not execute workflow code")
        ),
    )

    terminal = run_orchestrator(
        tmp_path,
        run.id,
        lease_seconds=2,
        recover=True,
        backend_absent_confirmed=True,
    )

    run.refresh()
    assert terminal == "succeeded"
    assert run.status == "succeeded"
    assert Storage(tmp_path).latest_success_run_id() == run.id


def test_recovery_converges_canonical_success_after_index_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = _expired_post_start(tmp_path, finalizing=True)
    original = Storage.update_latest_success_run

    def fail_index(self: Storage, run_id: str):
        raise OSError(f"injected latest-success failure for {run_id}")

    monkeypatch.setattr(Storage, "update_latest_success_run", fail_index)
    with pytest.raises(OSError, match="injected"):
        Storage(tmp_path).finalize_run_metadata(
            run.id,
            status="succeeded",
            update_latest_success=True,
        )
    assert Storage(tmp_path)._load_run_metadata(run.id)["status"] == "succeeded"
    assert run._control.read_status()["state"] == "finalizing"

    monkeypatch.setattr(Storage, "update_latest_success_run", original)
    terminal = run_orchestrator(
        tmp_path,
        run.id,
        lease_seconds=2,
        recover=True,
        backend_absent_confirmed=True,
    )

    run.refresh()
    assert terminal == "succeeded"
    assert run.status == "succeeded"
    assert Storage(tmp_path).latest_success_run_id() == run.id


def test_recovery_converges_canonical_cancellation_after_status_crash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = _expired_post_start(tmp_path, finalizing=False)
    Storage(tmp_path).write_run_metadata(
        run.id,
        workflow_identity="workflow:test",
        engine="parsl:parallel",
        status="running",
        target_nodes=[],
    )
    run.cancel()
    Storage(tmp_path).finalize_run_metadata(
        run.id,
        status="cancelled",
        update_latest_success=False,
    )
    monkeypatch.setattr(
        "bioimageflow.launcher.orchestrator._prepare_execution",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("recovery must not execute workflow code")
        ),
    )

    terminal = run_orchestrator(
        tmp_path,
        run.id,
        lease_seconds=2,
        recover=True,
        backend_absent_confirmed=True,
    )

    run.refresh()
    assert terminal == "cancelled"
    assert run.status == "cancelled"
    assert (
        Storage(tmp_path)._load_run_metadata(run.id)["status"]
        == "cancelled"
    )


def test_post_start_recovery_marks_missing_outcome_lost_without_rerun(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = _expired_post_start(tmp_path, finalizing=False)
    monkeypatch.setattr(
        "bioimageflow.launcher.orchestrator._prepare_execution",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("recovery must not execute workflow code")
        ),
    )

    terminal = run_orchestrator(
        tmp_path,
        run.id,
        lease_seconds=2,
        recover=True,
        backend_absent_confirmed=True,
    )

    run.refresh()
    assert terminal == "lost"
    assert run.status == "lost"
    assert (run.control_dir / "error.json").is_file()
