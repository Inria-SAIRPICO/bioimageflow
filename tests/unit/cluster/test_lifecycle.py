from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from bioimageflow.cluster.client import RemoteCluster
from bioimageflow.cluster.plan import RemoteExecutionPlan
from bioimageflow.cluster.reports import (
    ClusterCleanupPlan,
    ClusterOperationError,
    ClusterValidationReport,
    RemoteSubmissionUncertainError,
)
from bioimageflow.cluster.run import RemoteRunObservation
from bioimageflow.launcher.schemas import validate_run_id
from bioimageflow.workflow import Workflow

from tests.unit.cluster.lifecycle_support import (
    AmbiguousSubmissionTransport,
    MockGatewayTransport,
    _DIGEST_A,
    _DIGEST_B,
    _RUN_ID,
    _binding,
    _configured_cluster,
    _deployment,
    _plan,
    _validation,
)
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

    deployment = cluster.deploy(progress=lambda _event: None)
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
    assert validate_run_id(plan.run_id) == plan.run_id
    assert plan.storage_path == "/cluster/alice/bioimageflow/results/workflow"
    assert plan.validation_expires_at == validation.expires_at
    assert plan.executor_claims == validation.executor_bindings
    assert plan.validation_evidence == validation.evidence

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


def test_plan_refreshes_a_stale_validation_report(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = MockGatewayTransport()
    cluster = _configured_cluster(transport)
    monkeypatch.setattr(
        "bioimageflow.cluster.client.plan_distributed_execution",
        lambda *args, **kwargs: SimpleNamespace(nodes=()),
    )
    prepared = cluster.prepare(Workflow(name="workflow"))
    stale = ClusterValidationReport(
        _DIGEST_A,
        True,
        None,
        "2000-01-01T00:00:00Z",
        executor_bindings={"cpu": _binding().to_dict()},
        evidence={"external_attestation_digest": _DIGEST_B},
    )

    plan = cluster.plan(
        prepared,
        deployment=_deployment(cluster),
        validation=stale,
    )

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
    changed_claims = {**payload, "executor_claims": {}}
    with pytest.raises(ValueError, match="digest mismatch"):
        RemoteExecutionPlan.from_dict(changed_claims)

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
    with pytest.raises(TypeError, match="progress must be a callable"):
        cluster.deploy(progress="not-a-callback")
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


def test_ambiguous_submission_preserves_same_live_plan_for_recovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = AmbiguousSubmissionTransport()
    cluster = _configured_cluster(transport)
    monkeypatch.setattr(
        "bioimageflow.cluster.client.plan_distributed_execution",
        lambda *args, **kwargs: SimpleNamespace(nodes=()),
    )

    with pytest.raises(RemoteSubmissionUncertainError) as raised:
        cluster.submit(Workflow(name="workflow"))

    error = raised.value
    assert error.host == cluster.host
    assert error.root == str(cluster.root)
    assert error.run_id == error.plan.run_id
    assert error.attempt_id == error.plan.attempt_id
    assert error.category == "submission-uncertain"
    assert error.next_action == "attach-run-or-retry-same-plan"
    assert error.plan._sealed
    assert error.plan._prepared is not None
    assert error.plan._prepared._lease == error.attempt_id

    transport.fail_submission = False
    recovered = error.plan.submit()
    assert recovered.id == error.run_id
    assert error.plan.closed
    assert error.plan._prepared._lease is None
    error.plan._prepared.close()


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


def test_run_observations_are_exact_versioned_and_revision_guarded() -> None:
    transport = MockGatewayTransport()
    cluster = RemoteCluster(host="login.example", root="/cluster/alice/bioimageflow")
    cluster._transport_value = transport
    payload = transport.observation(_RUN_ID)

    observation = RemoteRunObservation.from_dict(payload)
    assert observation.to_dict() == payload
    with pytest.raises(ValueError):
        RemoteRunObservation.from_dict({**payload, "unexpected": True})

    run = cluster.attach(_RUN_ID)
    newer = {**payload, "status_revision": 2, "updated_at": "2026-08-24T12:00:01Z"}
    run._apply_observation(newer)
    with pytest.raises(ValueError, match="backwards"):
        run._apply_observation(payload)


def test_progress_pages_and_events_are_strict_and_monotonic() -> None:
    class ProgressTransport(MockGatewayTransport):
        def request(
            self,
            operation: str,
            arguments: dict[str, Any],
            *,
            operation_id: str | None = None,
        ) -> dict[str, Any]:
            if operation != "read-progress":
                return super().request(
                    operation,
                    arguments,
                    operation_id=operation_id,
                )
            observation = self.observation(arguments["run_id"])
            return {
                **observation,
                "events": [
                    {
                        "schema": "bioimageflow.launcher.progress.v1",
                        "run_id": arguments["run_id"],
                        "sequence": 1,
                        "timestamp": "2026-08-24T12:00:00Z",
                        "kind": "backend",
                        "payload": {
                            "schema": "bioimageflow.launcher.backend_event.v1",
                            "event": "orchestrator_running",
                            "owner": "worker-1",
                        },
                    }
                ],
                "has_more": False,
                "next_sequence": 1,
            }

    transport = ProgressTransport()
    cluster = RemoteCluster(host="login.example", root="/cluster/alice/bioimageflow")
    cluster._transport_value = transport
    run = cluster.attach(_RUN_ID)

    assert [event["sequence"] for event in run.progress()] == [1]


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


@pytest.mark.parametrize("path", ["foo/../bar", "a//b", "a/./b", "/absolute"])
def test_cleanup_candidates_require_confined_normalized_paths(path: str) -> None:
    from bioimageflow.cluster.reports import ClusterCleanupCandidate

    with pytest.raises(ValueError, match="normalized relative"):
        ClusterCleanupCandidate("temporary", _DIGEST_A, path, 0)
