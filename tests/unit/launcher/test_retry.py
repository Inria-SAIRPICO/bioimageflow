from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest

from bioimageflow import (
    ExecutorBinding,
    ExecutorCapabilities,
    OrchestratorLaunchConfig,
    ParslConfigRef,
    PSIJSubmissionUncertainError,
    RecomputeRequest,
    RunRetryPlan,
    WorkerEnvironmentAttestation,
    WorkerSlotCapacity,
    Workflow,
    WorkflowRunRetryError,
    submit_workflow,
)
from bioimageflow.launcher.orchestrator import run_orchestrator
from bioimageflow.launcher.artifacts import build_error_payload
from bioimageflow.launcher.cluster_agent import run_agent
from bioimageflow.launcher.cluster_protocol import request as cluster_request
from bioimageflow.storage import canonical_json_bytes
from bioimageflow.launcher.repository import LauncherRepository, _atomic_write_json
from bioimageflow.storage import Storage
from bioimageflow_common_tools import Concat, Generate


def _binding() -> ExecutorBinding:
    return ExecutorBinding(
        label="threads",
        environments=(
            WorkerEnvironmentAttestation(
                name="default",
                dependency_hash="0" * 64,
                allow_flexible_versions=False,
                core_requirement="bioimageflow-core>=0.2.1,<0.3",
            ),
        ),
        capabilities=ExecutorCapabilities(
            storage_modes=("shared_fs",),
            tool_origin_modes=("installed_module",),
            slot=WorkerSlotCapacity(cpu=1),
        ),
    )


def _submit(workflow: Workflow):
    return submit_workflow(
        workflow,
        parsl_config=ParslConfigRef(
            "tests.unit.launcher.config_factories:build",
            {"workers": 1},
        ),
        executor_bindings={"threads": _binding()},
        launch=OrchestratorLaunchConfig(backend="manual"),
    )


def _nested_workflow(storage: Path) -> Workflow:
    child = Workflow(storage_path=storage, engine="direct")
    with child:
        generated = Generate()(column_name="value", values=[1], name="generate")
        filtered = Concat()(generated, name="filter")
        child.output("value", filtered["value"], id="child-value")
    workflow = Workflow(storage_path=storage, engine="direct")
    with workflow:
        nested = child(name="nested")
        workflow.output("value", nested["value"], id="root-value")
    return workflow


def test_retry_plan_round_trip_and_retained_submission_clone(tmp_path: Path) -> None:
    workflow = _nested_workflow(tmp_path / "storage")
    parent = _submit(workflow)
    assert run_orchestrator(workflow.storage_path, parent.id) == "succeeded"

    prepared = parent.prepare_retry()
    assert RunRetryPlan.from_dict(
        json.loads(json.dumps(prepared.plan.to_dict()))
    ) == prepared.plan
    serialized = json.loads(json.dumps(prepared.plan.to_dict()))
    reopened = type(parent).open(workflow.storage_path, parent.id)
    retry = reopened.submit_retry(RunRetryPlan.from_dict(serialized))

    assert retry.id != parent.id
    assert retry.parent_id == parent.id
    assert retry.retry_plan == prepared.plan
    assert retry.status == "prepared"
    original = parent._control.read_submission()
    cloned = retry._control.read_submission()
    for field in (
        "workflow",
        "invocation",
        "parsl_config",
        "executor_bindings",
        "node_routes",
        "environment_routes",
        "shared_runtime_root",
        "task_policy",
        "launch",
        "psij_pre_launch",
    ):
        assert cloned[field] == original[field]
    assert run_orchestrator(workflow.storage_path, retry.id) == "succeeded"
    assert any(
        entry["kind"] == "public" and entry["payload"]["status"] == "cached"
        for entry in retry.progress()
    )


def test_retry_plan_detects_retained_submission_and_input_tampering(
    tmp_path: Path,
) -> None:
    storage = tmp_path / "storage"
    parent = _submit(Workflow(storage_path=storage, engine="direct"))
    assert run_orchestrator(storage, parent.id) == "succeeded"
    prepared = parent.prepare_retry()
    inputs = parent.control_dir / "inputs"
    inputs.mkdir(exist_ok=True)
    (inputs / "late.bin").write_bytes(b"not-confirmed")

    with pytest.raises(WorkflowRunRetryError, match="material changed"):
        prepared.submit()

    second = parent.prepare_retry()
    submission = parent._control.read_submission()
    submission["task_policy"]["max_in_flight"] -= 1
    _atomic_write_json(parent.control_dir / "submission.json", submission)

    with pytest.raises(WorkflowRunRetryError, match="material changed"):
        second.submit()


@pytest.mark.parametrize("state", ["succeeded", "failed", "cancelled", "lost"])
def test_every_terminal_state_has_an_explicit_retry_path(
    tmp_path: Path,
    state: str,
) -> None:
    storage = tmp_path / state
    parent = _submit(Workflow(storage_path=storage, engine="direct"))
    assert run_orchestrator(storage, parent.id) == "succeeded"
    status = parent._control.read_status()
    status["state"] = state
    status["revision"] += 1
    if state in {"failed", "lost"}:
        status["error"] = "error.json"
        _atomic_write_json(
            parent.control_dir / "error.json",
            build_error_payload(
                parent.id,
                code="test-terminal-state",
                error=RuntimeError(state),
                backend={"name": "manual"},
            ),
        )
    if state == "cancelled":
        status["cancel_requested_at"] = status["updated_at"]
    _atomic_write_json(parent.control_dir / "status.json", status)

    prepared = parent.prepare_retry()

    assert prepared.plan.parent_status == state


def test_nested_recompute_preview_is_revision_bound(tmp_path: Path) -> None:
    workflow = _nested_workflow(tmp_path / "storage")
    parent = _submit(workflow)
    assert run_orchestrator(workflow.storage_path, parent.id) == "succeeded"
    request = RecomputeRequest(("nested/generate",), cascade=False)

    prepared = parent.prepare_retry(request)

    assert {item.node_path for item in prepared.plan.invalidations} == {
        "nested/generate"
    }
    workflow.invalidate(request.node_paths, cascade=request.cascade)
    with pytest.raises(WorkflowRunRetryError, match="Cache selections changed"):
        prepared.submit()


def test_nested_recompute_cascades_and_applies_exact_preview(tmp_path: Path) -> None:
    workflow = _nested_workflow(tmp_path / "storage")
    parent = _submit(workflow)
    assert run_orchestrator(workflow.storage_path, parent.id) == "succeeded"
    prepared = parent.prepare_retry(
        RecomputeRequest(("nested/generate",), cascade=True)
    )

    assert {item.node_path for item in prepared.plan.invalidations} == {
        "nested/filter",
        "nested/generate",
    }
    retry = prepared.submit()
    assert all(
        not (
            Storage(workflow.storage_path).result_dir(item.result_key)
            / "current.json"
        ).exists()
        for item in prepared.plan.invalidations
    )
    assert run_orchestrator(workflow.storage_path, retry.id) == "succeeded"


def test_retry_refuses_conflicting_active_run(tmp_path: Path) -> None:
    storage = tmp_path / "storage"
    parent = _submit(Workflow(storage_path=storage, engine="direct"))
    assert run_orchestrator(storage, parent.id) == "succeeded"
    active = _submit(Workflow(storage_path=storage, engine="direct"))

    prepared = parent.prepare_retry()

    assert prepared.plan.conflicting_run_ids == (active.id,)
    with pytest.raises(WorkflowRunRetryError, match="active execution"):
        prepared.submit()


def test_retry_refuses_an_active_attached_execution(tmp_path: Path) -> None:
    storage_path = tmp_path / "storage"
    parent = _submit(Workflow(storage_path=storage_path, engine="direct"))
    assert run_orchestrator(storage_path, parent.id) == "succeeded"
    attached_id = f"run_{uuid.uuid4().hex}"
    Storage(storage_path).start_run_metadata(
        attached_id,
        workflow_identity="attached",
        engine="direct:parallel",
        target_nodes=(),
        launcher_reserved=False,
    )

    prepared = parent.prepare_retry()

    assert prepared.plan.conflicting_run_ids == (attached_id,)
    assert prepared.can_submit is False
    with pytest.raises(WorkflowRunRetryError, match="active execution"):
        prepared.submit()


def test_recompute_rolls_back_invalidation_when_child_allocation_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow = _nested_workflow(tmp_path / "storage")
    parent = _submit(workflow)
    assert run_orchestrator(workflow.storage_path, parent.id) == "succeeded"
    prepared = parent.prepare_retry(
        RecomputeRequest(("nested/generate",), cascade=False)
    )
    result_key = prepared.plan.invalidations[0].result_key
    current = Storage(workflow.storage_path).result_dir(result_key) / "current.json"
    original = current.read_bytes()

    monkeypatch.setattr(
        LauncherRepository,
        "allocate",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("allocation failed")),
    )

    with pytest.raises(OSError, match="allocation failed"):
        prepared.submit()
    assert current.read_bytes() == original


def test_recompute_resumes_a_partially_journaled_invalidation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow = _nested_workflow(tmp_path / "storage")
    parent = _submit(workflow)
    assert run_orchestrator(workflow.storage_path, parent.id) == "succeeded"
    plan = parent.prepare_retry(
        RecomputeRequest(("nested/generate",), cascade=True)
    ).plan
    from bioimageflow.launcher import retry as retry_module

    original_write = retry_module._write_retry_transaction
    interrupted = False

    def interrupt_invalidated(control, retry_plan, phase):
        nonlocal interrupted
        if phase == "invalidated" and not interrupted:
            interrupted = True
            raise OSError("simulated process loss")
        original_write(control, retry_plan, phase)

    monkeypatch.setattr(
        retry_module,
        "_write_retry_transaction",
        interrupt_invalidated,
    )
    with pytest.raises(OSError, match="process loss"):
        parent.submit_retry(plan)

    child = parent.submit_retry(plan)

    assert child.id == plan.retry_run_id
    assert all(
        not (
            Storage(workflow.storage_path).result_dir(item.result_key)
            / "current.json"
        ).exists()
        for item in plan.invalidations
    )


def test_recompute_journal_removes_a_malformed_current_pointer(
    tmp_path: Path,
) -> None:
    workflow = _nested_workflow(tmp_path / "storage")
    parent = _submit(workflow)
    assert run_orchestrator(workflow.storage_path, parent.id) == "succeeded"
    initial = parent.prepare_retry(
        RecomputeRequest(("nested/generate",), cascade=False)
    ).plan.invalidations[0]
    current = Storage(workflow.storage_path).result_dir(initial.result_key) / "current.json"
    current.write_text("{")
    prepared = parent.prepare_retry(
        RecomputeRequest(("nested/generate",), cascade=False)
    )

    assert prepared.plan.invalidations[0].selection_status == "corrupt"
    assert prepared.plan.invalidations[0].record_id is None
    child = prepared.submit()

    assert child.id == prepared.plan.retry_run_id
    assert not current.exists()


def test_retry_resumes_an_allocated_child_before_dispatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = tmp_path / "storage"
    parent = _submit(Workflow(storage_path=storage, engine="direct"))
    assert run_orchestrator(storage, parent.id) == "succeeded"
    plan = parent.prepare_retry().plan
    from bioimageflow.launcher import retry as retry_module

    original_launch = retry_module._launch_or_reconnect_retry
    interrupted = False

    def interrupt_once(control, launch, config_ref):
        nonlocal interrupted
        if not interrupted:
            interrupted = True
            raise OSError("simulated process loss")
        return original_launch(control, launch, config_ref)

    monkeypatch.setattr(
        retry_module,
        "_launch_or_reconnect_retry",
        interrupt_once,
    )
    with pytest.raises(OSError, match="process loss"):
        parent.submit_retry(plan)

    child = parent.submit_retry(plan)

    assert child.id == plan.retry_run_id
    assert (child.control_dir / "command.json").is_file()


def test_nonterminal_parent_cannot_be_retried(tmp_path: Path) -> None:
    run = _submit(Workflow(storage_path=tmp_path / "storage", engine="direct"))

    with pytest.raises(WorkflowRunRetryError, match="terminal"):
        run.prepare_retry()


def test_retry_rejects_stale_parent_status_revision(tmp_path: Path) -> None:
    storage = tmp_path / "storage"
    parent = _submit(Workflow(storage_path=storage, engine="direct"))
    assert run_orchestrator(storage, parent.id) == "succeeded"
    prepared = parent.prepare_retry()
    status = parent._control.read_status()
    status["revision"] += 1
    _atomic_write_json(parent.control_dir / "status.json", status)

    with pytest.raises(WorkflowRunRetryError, match="revision"):
        prepared.submit()


def test_cluster_agent_retry_is_idempotent_and_reconnectable(
    tmp_path: Path,
    monkeypatch,
) -> None:
    storage = tmp_path / "storage"
    parent = _submit(Workflow(storage_path=storage, engine="direct"))
    assert run_orchestrator(storage, parent.id) == "succeeded"
    launches: list[str] = []
    monkeypatch.setattr(
        "bioimageflow.launcher.backends.launch_orchestrator",
        lambda control, launch, *, secret_refs: launches.append(control.run_id),
    )

    def call(operation: str, arguments: dict, request_id: str | None = None) -> dict:
        encoded = canonical_json_bytes(
            cluster_request(
                operation,
                arguments,
                request_id=request_id or str(uuid.uuid4()),
            )
        )
        response = json.loads(run_agent(encoded))
        assert response["ok"] is True, response
        return response["result"]

    plan = call(
        "prepare-retry",
        {
            "run_id": parent.id,
            "storage_path": storage.as_posix(),
            "recompute": None,
        },
    )
    retry_id = str(uuid.uuid4())
    first = call(
        "retry",
        {"storage_path": storage.as_posix(), "plan": plan},
        retry_id,
    )
    second = call(
        "retry",
        {"storage_path": storage.as_posix(), "plan": plan},
        retry_id,
    )

    assert first == second
    assert first["run_id"] == plan["retry_run_id"]
    assert launches == [plan["retry_run_id"]]


def test_retry_submission_uncertainty_is_never_resubmitted(
    tmp_path: Path,
    monkeypatch,
) -> None:
    storage = tmp_path / "storage"
    parent = _submit(Workflow(storage_path=storage, engine="direct"))
    assert run_orchestrator(storage, parent.id) == "succeeded"
    prepared = parent.prepare_retry()
    calls = 0

    def uncertain(*args, **kwargs):
        nonlocal calls
        del args, kwargs
        calls += 1
        raise PSIJSubmissionUncertainError("scheduler outcome unknown")

    monkeypatch.setattr(
        "bioimageflow.launcher.retry._launch_prepared_control",
        uncertain,
    )

    with pytest.raises(PSIJSubmissionUncertainError):
        prepared.submit()
    with pytest.raises(RuntimeError, match="already submitted"):
        prepared.submit()

    assert calls == 1
    retained = parent.open(storage, prepared.plan.retry_run_id)
    assert retained.parent_id == parent.id
    assert parent.submit_retry(prepared.plan).id == retained.id
    assert calls == 1
