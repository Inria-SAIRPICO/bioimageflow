from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from bioimageflow.launcher.inputs import INVOCATION_SCHEMA
from bioimageflow.launcher.schemas import SUBMISSION_SCHEMA, utc_timestamp
from bioimageflow.storage import canonical_json_bytes


def launcher_submission(
    storage_root: Path,
    run_id: str,
    *,
    backend: str = "local",
    hard_cancel_after: float | None = None,
    created_at: str | None = None,
) -> dict[str, Any]:
    workflow_payload = {"schema_version": 1}
    workflow_digest = hashlib.sha256(canonical_json_bytes(workflow_payload)).hexdigest()
    return {
        "schema": SUBMISSION_SCHEMA,
        "run_id": run_id,
        "created_at": created_at or utc_timestamp(),
        "storage_root": str(storage_root.resolve()),
        "canonical_view": f"views/runs/{run_id}",
        "workflow": {
            "kind": "graph_v1",
            "digest": f"sha256:{workflow_digest}",
            "payload": workflow_payload,
        },
        "invocation": {
            "schema": INVOCATION_SCHEMA,
            "variant": "root",
            "inputs": [],
            "outputs": [],
        },
        "parsl_config": {
            "factory": "tests.unit.launcher.config_factories:build",
            "kwargs": {"workers": 1},
            "secret_refs": None,
        },
        "executor_bindings": {
            "threads": {
                "schema": "bioimageflow.parsl.executor_binding.v1",
                "label": "threads",
                "environments": [
                    {
                        "schema": (
                            "bioimageflow.parsl.worker_environment_attestation.v1"
                        ),
                        "name": "default",
                        "dependency_hash": "0" * 64,
                        "allow_flexible_versions": False,
                        "core_requirement": "bioimageflow-core==0.1.7",
                    }
                ],
                "capabilities": {
                    "schema": "bioimageflow.parsl.executor_capabilities.v1",
                    "storage_modes": ["shared_fs"],
                    "tool_origin_modes": ["installed_module"],
                    "slot": {
                        "schema": ("bioimageflow.parsl.worker_slot_capacity.v1"),
                        "cpu": 1,
                        "gpu": 0,
                        "memory_bytes": None,
                        "gpu_memory_bytes": None,
                    },
                },
            }
        },
        "node_routes": None,
        "environment_routes": None,
        "shared_runtime_root": None,
        "task_policy": {
            "schema": "bioimageflow.parsl.task_policy.v1",
            "row_chunk_size": 1,
            "max_in_flight": 32,
        },
        "launch": {
            "backend": backend,
            "work_dir": None,
            "hard_cancel_after": hard_cancel_after,
        },
        "psij_pre_launch": None,
        "protocol_versions": {
            "launcher": 1,
            "workflow_graph": 1,
            "workflow_archive": 1,
            "parsl_task": 1,
            "parsl_result": 1,
        },
        "retry_plan": None,
    }


def public_progress_payload() -> dict[str, Any]:
    return {
        "schema": "bioimageflow.progress_event.v1",
        "node_name": "test-node",
        "status": "running",
        "row": 0,
        "total_rows": 1,
        "message": None,
        "current": None,
        "maximum": None,
        "timestamp": 0.0,
        "result_key": None,
        "record_id": None,
    }


def backend_progress_payload(
    *,
    event: str = "orchestrator_running",
    owner: str = "test-owner",
) -> dict[str, Any]:
    return {
        "schema": "bioimageflow.launcher.backend_event.v1",
        "event": event,
        "owner": owner,
    }
