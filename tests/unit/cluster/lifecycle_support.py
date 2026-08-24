from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from bioimageflow.cluster.client import RemoteCluster
from bioimageflow.cluster.plan import RemoteExecutionPlan
from bioimageflow.cluster.reports import (
    ClusterDeployment,
    ClusterValidationReport,
)
from bioimageflow.cluster.transport import GatewayTransportError
from bioimageflow.cluster.values import (
    ClusterEnvironment,
    ParslConfiguration,
    SchedulerJob,
)
from bioimageflow.parsl import (
    ExecutorBinding,
    ExecutorCapabilities,
    WorkerEnvironmentAttestation,
    WorkerSlotCapacity,
)
from bioimageflow.workflow import Workflow


_RUN_ID = "run_12345678123442348234123456789abc"
_DIGEST_A = "sha256:" + "a" * 64
_DIGEST_B = "sha256:" + "b" * 64


class MockGatewayTransport:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any], str | None]] = []
        self.run_state = "running"
        self.status_revision = 0

    def observation(
        self,
        run_id: str,
        *,
        state: str | None = None,
        storage_path: str = "/cluster/alice/bioimageflow/results/workflow",
    ) -> dict[str, Any]:
        observed_state = self.run_state if state is None else state
        return {
            "schema": "bioimageflow.launcher.run-observation.v1",
            "error": None,
            "retry_plan": None,
            "run_id": run_id,
            "state": observed_state,
            "status_revision": self.status_revision,
            "storage_path": storage_path,
            "terminal": observed_state in {"succeeded", "failed", "cancelled", "lost"},
            "updated_at": "2026-08-24T12:00:00Z",
            "attempt_phase": "submitted",
            "gateway_publication_id": "gateway-test",
            "gateway_artifact_digest": _DIGEST_B,
        }

    def check_connection(self) -> dict[str, Any]:
        self.calls.append(("check-connection", {}, None))
        return {
            "schema": "bioimageflow.cluster_connection_report.v1",
            "reachable": True,
            "gateway_available": True,
            "bootstrap_required": False,
            "gateway_version": "1",
            "protocol_versions": [1],
            "diagnostics": [],
        }

    def publish_deployment(self, prepared: Any, *, progress: Any = None) -> dict[str, Any]:
        self.calls.append(
            (
                "publish-deployment",
                {"deployment_id": prepared.deployment_id, "progress": progress},
                None,
            )
        )
        return {
            "deployment_id": prepared.deployment_id,
            "manifest_digest": prepared.manifest["manifest_digest"],
            "ownership": "external",
            "reused": False,
            "environment_kind": "existing_python",
            "gateway_publication_id": "gateway-v1",
            "external_attestation_digest": _DIGEST_B,
        }

    def submit_plan(self, plan: Any, prepared: Any) -> dict[str, Any]:
        prepared._verify()
        self.calls.append(
            (
                "submit-plan",
                {"attempt_id": plan.attempt_id, "run_id": plan.run_id},
                plan.attempt_id,
            )
        )
        return {
            "run_id": plan.run_id,
            "observation": self.observation(
                plan.run_id,
                state="starting",
                storage_path=plan.storage_path,
            ),
        }

    def request(
        self,
        operation: str,
        arguments: dict[str, Any],
        *,
        operation_id: str | None = None,
    ) -> dict[str, Any]:
        self.calls.append((operation, arguments, operation_id))
        if operation == "validate-deployment":
            return _validation(arguments["deployment_id"]).to_dict()
        if operation in {"inspect-run", "refresh-run"}:
            return self.observation(arguments["run_id"])
        if operation == "cancel-run":
            self.run_state = "cancelled"
            self.status_revision += 1
            return self.observation(arguments["run_id"])
        if operation == "plan-cleanup":
            payload = {
                "schema": "bioimageflow.cluster_cleanup_plan.v1",
                "root_revision": 7,
                "candidates": [
                    {
                        "namespace": "temporary",
                        "identity": _DIGEST_A,
                        "path": "temporary/abandoned",
                        "size": 12,
                        "reference_reasons": [],
                        "consequences": [],
                    }
                ],
            }
            return {**payload, "plan_id": _DIGEST_A}
        if operation == "apply-cleanup":
            return {
                "schema": "bioimageflow.cluster_cleanup_report.v1",
                "plan_id": arguments["plan"]["plan_id"],
                "removed": [_DIGEST_A],
                "skipped": {},
            }
        raise AssertionError(f"Unexpected gateway operation: {operation}")


class AmbiguousSubmissionTransport(MockGatewayTransport):
    def __init__(self) -> None:
        super().__init__()
        self.fail_submission = True

    def submit_plan(self, plan: Any, prepared: Any) -> dict[str, Any]:
        if self.fail_submission:
            raise GatewayTransportError(
                "ssh-timeout",
                "The submission acknowledgement was lost.",
                ambiguous=True,
            )
        return super().submit_plan(plan, prepared)


def _binding() -> ExecutorBinding:
    return ExecutorBinding(
        label="cpu",
        environments=(
            WorkerEnvironmentAttestation(
                name="default",
                dependency_hash="a" * 64,
                allow_flexible_versions=False,
                core_requirement="bioimageflow-core>=0.1.7,<0.2",
            ),
        ),
        capabilities=ExecutorCapabilities(
            storage_modes=("shared_fs",),
            tool_origin_modes=("installed_module",),
            slot=WorkerSlotCapacity(cpu=1),
        ),
    )


def _validation(deployment_id: str = _DIGEST_A) -> ClusterValidationReport:
    return ClusterValidationReport(
        deployment_id=deployment_id,
        valid=True,
        validation_digest=None,
        expires_at="2999-01-01T00:00:00Z",
        executor_bindings={"cpu": _binding().to_dict()},
        evidence={"external_attestation_digest": _DIGEST_B},
    )


def _configured_cluster(transport: MockGatewayTransport) -> RemoteCluster:
    cluster = RemoteCluster(
        host="login.example",
        root="/cluster/alice/bioimageflow",
        environment=ClusterEnvironment.from_existing_python("/opt/python/bin/python3"),
        parsl=ParslConfiguration.from_module("site.parsl:build"),
        orchestrator=SchedulerJob("slurm", timedelta(hours=1)),
    )
    cluster._transport_value = transport
    return cluster


def _deployment(cluster: RemoteCluster, deployment_id: str = _DIGEST_A) -> ClusterDeployment:
    return ClusterDeployment(
        deployment_id,
        _DIGEST_B,
        "external",
        False,
        "existing_python",
        "gateway-v1",
        _DIGEST_B,
        _cluster=cluster,
    )


def _plan(
    cluster: RemoteCluster,
    monkeypatch: pytest.MonkeyPatch,
    *,
    workflow_name: str = "workflow",
) -> tuple[Any, RemoteExecutionPlan]:
    monkeypatch.setattr(
        "bioimageflow.cluster.client.plan_distributed_execution",
        lambda *args, **kwargs: SimpleNamespace(nodes=()),
    )
    prepared = cluster.prepare(Workflow(name=workflow_name))
    plan = cluster.plan(
        prepared,
        deployment=_deployment(cluster),
        validation=_validation(),
    )
    return prepared, plan



__all__ = [
    "AmbiguousSubmissionTransport",
    "MockGatewayTransport",
    "_DIGEST_A",
    "_DIGEST_B",
    "_RUN_ID",
    "_binding",
    "_configured_cluster",
    "_deployment",
    "_plan",
    "_validation",
]
