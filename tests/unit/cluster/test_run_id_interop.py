from __future__ import annotations

import json
import sys
from datetime import timedelta
from pathlib import Path
from typing import Any

from bioimageflow import Workflow
from bioimageflow.cluster.client import RemoteCluster
from bioimageflow.cluster.gateway import _RUN_SUBMITTER_SCRIPT
from bioimageflow.cluster.run import RemoteWorkflowRun
from bioimageflow.launcher.payload import serialize_workflow_payload
from bioimageflow.launcher.retry import RunRetryPlan
from bioimageflow.launcher.schemas import new_run_id, validate_run_id
from bioimageflow.launcher.types import PSIJLaunchConfig, ParslConfigRef
from bioimageflow.parsl import (
    ExecutorBinding,
    ExecutorCapabilities,
    ParslTaskPolicy,
    WorkerEnvironmentAttestation,
    WorkerSlotCapacity,
)


def _observation(run_id: str, storage_path: Path) -> dict[str, Any]:
    return {
        "schema": "bioimageflow.launcher.run-observation.v1",
        "attempt_phase": "submitted",
        "error": None,
        "gateway_artifact_digest": "sha256:" + "a" * 64,
        "gateway_publication_id": "gateway-v1",
        "retry_plan": None,
        "run_id": run_id,
        "state": "succeeded",
        "status_revision": 4,
        "storage_path": storage_path.as_posix(),
        "terminal": True,
        "updated_at": "2026-08-24T12:00:00Z",
    }


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


def test_managed_run_id_crosses_the_real_gateway_child_submission_boundary(
    tmp_path: Path, monkeypatch: Any, capsys: Any
) -> None:
    storage_path = tmp_path / "storage"
    invocation_root = tmp_path / "invocation"
    deployment_content = tmp_path / "deployment" / "content"
    shared_runtime_root = tmp_path / "runtime"
    invocation_root.mkdir()
    (deployment_content / "parsl").mkdir(parents=True)
    shared_runtime_root.mkdir()
    workflow = Workflow(name="managed-child-boundary")
    run_id = new_run_id()
    (invocation_root / "invocation.json").write_text(
        json.dumps(
            {
                "workflow": serialize_workflow_payload(workflow),
                "inputs": [],
                "targets": None,
                "node_input_overrides": [],
                "task_policy": ParslTaskPolicy().to_dict(),
            }
        )
    )
    request_path = tmp_path / "request.json"
    request_path.write_text(
        json.dumps(
            {
                "run_id": run_id,
                "storage_path": storage_path.as_posix(),
                "invocation_root": invocation_root.as_posix(),
                "deployment_content": deployment_content.as_posix(),
                "shared_runtime_root": shared_runtime_root.as_posix(),
                "parsl_config": ParslConfigRef(
                    "tests.unit.launcher.config_factories:build", {"workers": 1}
                ).to_dict(),
                "executor_bindings": {"threads": _binding().to_dict()},
                "node_routes": {},
                "launch": PSIJLaunchConfig(
                    executor="slurm", walltime=timedelta(minutes=5)
                ).to_dict(),
                "pre_launch": "true\n",
            }
        )
    )
    monkeypatch.setattr(
        "bioimageflow.launcher.backends.launch_orchestrator",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(sys, "argv", ["submit_run.py", str(request_path)])
    original_path = list(sys.path)
    try:
        exec(compile(_RUN_SUBMITTER_SCRIPT, "submit_run.py", "exec"), {"__name__": "__main__"})
    finally:
        sys.path[:] = original_path

    response = json.loads(capsys.readouterr().out)
    assert response["status"] == "ok"
    assert response["payload"]["run_id"] == run_id
    assert validate_run_id(response["payload"]["run_id"]) == run_id
    assert (storage_path / "launcher" / "v1" / "runs" / run_id).is_dir()


class _RetryTransport:
    def __init__(self, retry_run_id: str, storage_path: Path) -> None:
        self.retry_run_id = retry_run_id
        self.storage_path = storage_path

    def request(
        self,
        operation: str,
        arguments: dict[str, Any],
        *,
        operation_id: str | None = None,
    ) -> dict[str, Any]:
        assert operation == "start-retry"
        assert operation_id == self.retry_run_id
        assert arguments["plan"]["retry_run_id"] == self.retry_run_id
        return _observation(self.retry_run_id, self.storage_path)


def test_managed_retry_result_accepts_the_launcher_retry_run_id(tmp_path: Path) -> None:
    parent_run_id = new_run_id()
    retry_run_id = new_run_id()
    cluster = RemoteCluster(host="cluster", root="/cluster/bioimageflow")
    cluster._transport_value = _RetryTransport(retry_run_id, tmp_path)
    parent = RemoteWorkflowRun(
        cluster,
        parent_run_id,
        _observation(parent_run_id, tmp_path),
    )
    digest = "sha256:" + "1" * 64
    plan = RunRetryPlan(
        parent_run_id=parent_run_id,
        retry_run_id=retry_run_id,
        parent_status="succeeded",
        parent_status_revision=4,
        storage_path=tmp_path.as_posix(),
        retained_submission_digest=digest,
        retained_material_digest=digest,
        retained_material_entries=0,
        cache_selection_revision=digest,
        recompute=None,
        invalidations=(),
        conflicting_run_ids=(),
    )

    retry = parent.start_retry(plan)

    assert retry.id == retry_run_id
    assert retry.status == "succeeded"
