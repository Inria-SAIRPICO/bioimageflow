from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from bioimageflow.cluster.client import RemoteCluster
from bioimageflow.cluster.plan import RemoteExecutionPlan
from bioimageflow.cluster.reports import (
    ClusterCleanupPlan,
    ClusterDeployment,
    ClusterOperationError,
    ClusterValidationReport,
)
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


_RUN_ID = "12345678-1234-4234-8234-123456789abc"
_DIGEST_A = "sha256:" + "a" * 64
_DIGEST_B = "sha256:" + "b" * 64


class MockGatewayTransport:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any], str | None]] = []
        self.run_state = "running"

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
            "observation": {"run_id": plan.run_id, "state": "starting"},
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
            return {"run_id": arguments["run_id"], "state": self.run_state}
        if operation == "cancel-run":
            self.run_state = "cancelled"
            return {"run_id": arguments["run_id"], "state": "cancelled"}
        if operation == "plan-cleanup":
            return {
                "schema": "bioimageflow.cluster_cleanup_plan.v1",
                "plan_id": "cleanup-1",
                "root_revision": 7,
                "candidates": [
                    {
                        "namespace": "temporary",
                        "identity": _DIGEST_A,
                        "path": "temporary/abandoned",
                        "size": 12,
                        "consequences": [],
                    }
                ],
            }
        if operation == "apply-cleanup":
            return {
                "schema": "bioimageflow.cluster_cleanup_report.v1",
                "plan_id": arguments["plan"]["plan_id"],
                "removed": [_DIGEST_A],
                "skipped": {},
            }
        raise AssertionError(f"Unexpected gateway operation: {operation}")


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
        validation_digest=_DIGEST_B,
        expires_at="2999-01-01T00:00:00Z",
        executor_bindings={"cpu": _binding().to_dict()},
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


def test_attach_only_cluster_allows_observation_but_rejects_configured_operations() -> None:
    transport = MockGatewayTransport()
    cluster = RemoteCluster(host="login.example", root="/cluster/alice/bioimageflow")
    cluster._transport_value = transport

    assert cluster.check_connection().valid
    assert cluster.attach(_RUN_ID).status == "running"
    transport.calls.clear()

    deployment = _deployment(cluster)
    operations = (
        ("deploy", lambda: cluster.deploy()),
        ("prepare", lambda: cluster.prepare(Workflow(name="workflow"))),
        ("validate", lambda: cluster.validate(deployment=deployment)),
        (
            "plan",
            lambda: cluster.plan(object(), deployment=deployment),  # type: ignore[arg-type]
        ),
        ("submit", lambda: cluster.submit(Workflow(name="workflow"))),
    )
    for phase, operation in operations:
        with pytest.raises(ClusterOperationError) as raised:
            operation()
        assert raised.value.diagnostic.phase == phase
        assert raised.value.diagnostic.category == "configuration-incomplete"
    assert transport.calls == []


def test_deploy_prepare_validate_and_confirmed_plan_have_bounded_effects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = MockGatewayTransport()
    cluster = _configured_cluster(transport)

    deployment = cluster.deploy(progress="progress-token")
    assert [call[0] for call in transport.calls] == ["publish-deployment"]

    transport.calls.clear()
    prepared = cluster.prepare(Workflow(name="workflow"))
    assert transport.calls == []

    validation = cluster.validate(deployment=deployment)
    assert [call[0] for call in transport.calls] == ["validate-deployment"]
    assert set(transport.calls[0][1]) == {
        "deployment_id",
        "scheduler_job",
        "timeout",
    }

    transport.calls.clear()
    monkeypatch.setattr(
        "bioimageflow.cluster.client.plan_distributed_execution",
        lambda *args, **kwargs: SimpleNamespace(nodes=()),
    )
    plan = cluster.plan(prepared, deployment=deployment, validation=validation)
    assert transport.calls == []
    assert plan.run_id != plan.attempt_id
    assert plan.storage_path == "/cluster/alice/bioimageflow/results/workflow"

    plan.close()
    prepared.close()


def test_plan_without_validation_refreshes_validation_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = MockGatewayTransport()
    cluster = _configured_cluster(transport)
    monkeypatch.setattr(
        "bioimageflow.cluster.client.plan_distributed_execution",
        lambda *args, **kwargs: SimpleNamespace(nodes=()),
    )
    prepared = cluster.prepare(Workflow(name="workflow"))

    plan = cluster.plan(prepared, deployment=_deployment(cluster))

    assert [call[0] for call in transport.calls] == ["validate-deployment"]
    plan.close()
    prepared.close()


def test_plan_digest_round_trip_is_stable_and_detached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = MockGatewayTransport()
    cluster = _configured_cluster(transport)
    prepared, plan = _plan(cluster, monkeypatch)
    payload = plan.to_dict()

    summary = RemoteExecutionPlan.from_dict(payload)

    assert summary.detached
    assert summary.plan_digest == plan.plan_digest
    assert summary.to_dict() == payload
    with pytest.raises(AttributeError, match="immutable"):
        plan.host = "other-login.example"
    with pytest.raises(RuntimeError, match="local-state-unavailable"):
        summary.submit()
    changed_target = {**payload, "host": "other-login.example"}
    with pytest.raises(ValueError, match="digest mismatch"):
        RemoteExecutionPlan.from_dict(changed_target)

    plan.close()
    prepared.close()


def test_lifecycle_timeouts_and_plan_lifetimes_must_be_finite() -> None:
    transport = MockGatewayTransport()
    cluster = _configured_cluster(transport)
    prepared = cluster.prepare(Workflow(name="workflow"))
    deployment = _deployment(cluster)

    with pytest.raises(ValueError, match="positive finite"):
        cluster.validate(deployment=deployment, timeout=float("inf"))
    with pytest.raises(ValueError, match="positive finite"):
        cluster.plan(
            prepared,
            deployment=deployment,
            validation=_validation(),
            lifetime=float("nan"),
        )
    assert transport.calls == []
    prepared.close()


def test_prepared_invocation_has_one_exclusive_plan_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = MockGatewayTransport()
    cluster = _configured_cluster(transport)
    prepared, first = _plan(cluster, monkeypatch)

    with pytest.raises(RuntimeError, match="active plan"):
        cluster.plan(
            prepared,
            deployment=_deployment(cluster),
            validation=_validation(),
        )
    with pytest.raises(RuntimeError, match="leased by an active plan"):
        prepared.close()

    first.close()
    second = cluster.plan(
        prepared,
        deployment=_deployment(cluster),
        validation=_validation(),
    )
    second.close()
    prepared.close()


def test_repeated_plan_submit_reuses_stable_attempt_and_run_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = MockGatewayTransport()
    cluster = _configured_cluster(transport)
    prepared, plan = _plan(cluster, monkeypatch)

    first = plan.submit()
    second = plan.submit()

    submit_calls = [call for call in transport.calls if call[0] == "submit-plan"]
    assert len(submit_calls) == 1
    assert submit_calls[0][1] == {
        "attempt_id": plan.attempt_id,
        "run_id": plan.run_id,
    }
    assert submit_calls[0][2] == plan.attempt_id
    assert second is first
    assert first.id == plan.run_id
    assert plan.closed
    assert prepared._lease is None
    prepared.close()


def test_attach_and_cancel_are_explicit_and_idempotent() -> None:
    transport = MockGatewayTransport()
    cluster = RemoteCluster(host="login.example", root="/cluster/alice/bioimageflow")
    cluster._transport_value = transport

    run = cluster.attach(_RUN_ID)
    assert [call[0] for call in transport.calls] == ["inspect-run"]

    run.cancel()
    run.cancel()

    assert run.status == "cancelled"
    assert [call[0] for call in transport.calls] == [
        "inspect-run",
        "cancel-run",
        "refresh-run",
    ]
    assert len([call for call in transport.calls if call[0] == "cancel-run"]) == 1


def test_cleanup_is_two_phase_and_bound_to_the_originating_cluster() -> None:
    transport = MockGatewayTransport()
    cluster = RemoteCluster(host="login.example", root="/cluster/alice/bioimageflow")
    cluster._transport_value = transport

    plan = cluster.plan_cleanup(namespace="temporary")
    assert isinstance(plan, ClusterCleanupPlan)
    assert [call[0] for call in transport.calls] == ["plan-cleanup"]

    report = cluster.apply_cleanup(plan)
    assert report.plan_id == plan.plan_id
    assert report.removed == (_DIGEST_A,)
    assert [call[0] for call in transport.calls] == [
        "plan-cleanup",
        "apply-cleanup",
    ]
    assert transport.calls[-1][2] == plan.plan_id

    other = RemoteCluster(host="other.example", root="/cluster/other/bioimageflow")
    with pytest.raises(ValueError, match="another cluster"):
        other.apply_cleanup(plan)
