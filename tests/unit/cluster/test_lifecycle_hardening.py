from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from bioimageflow.cluster.client import RemoteCluster
from bioimageflow.cluster.reports import (
    CLUSTER_DIAGNOSTIC_CATEGORIES,
    ClusterOperationError,
)
from bioimageflow.cluster.values import (
    ClusterEnvironment,
    ParslConfiguration,
    SchedulerJob,
)

from tests.unit.cluster.lifecycle_support import (
    MockGatewayTransport,
    _RUN_ID,
    _deployment,
)


def _resource_cluster(
    transport: MockGatewayTransport, **scheduler_options: Any
) -> RemoteCluster:
    cluster = RemoteCluster(
        host="login.example",
        root="/cluster/alice/bioimageflow",
        environment=ClusterEnvironment.from_existing_python("/opt/python/bin/python3"),
        parsl=ParslConfiguration.from_module("site.parsl:build"),
        orchestrator=SchedulerJob(
            "slurm", timedelta(hours=1), **scheduler_options
        ),
    )
    cluster._transport_value = transport
    return cluster


@pytest.mark.parametrize(
    "scheduler_options",
    [
        {"gpu": 1},
        {"memory": "1 GiB"},
        {"attributes": {"exclusive": True}},
    ],
)
def test_unsupported_orchestrator_resources_fail_before_validation_or_planning(
    scheduler_options: dict[str, Any],
) -> None:
    transport = MockGatewayTransport()
    cluster = _resource_cluster(transport, **scheduler_options)
    deployment = _deployment(cluster)

    with pytest.raises(ClusterOperationError) as validation:
        cluster.validate(deployment=deployment)
    with pytest.raises(ClusterOperationError) as planning:
        cluster.plan(object(), deployment=deployment)  # type: ignore[arg-type]

    assert validation.value.diagnostic.category == "unsupported-scheduler-adapter"
    assert planning.value.diagnostic.category == "unsupported-scheduler-adapter"
    assert transport.calls == []


def test_malformed_attach_and_progress_payloads_are_cluster_errors() -> None:
    class MalformedTransport(MockGatewayTransport):
        malformed_attach = True

        def request(
            self,
            operation: str,
            arguments: dict[str, Any],
            *,
            operation_id: str | None = None,
        ) -> dict[str, Any]:
            if operation == "inspect-run" and self.malformed_attach:
                return {"run_id": arguments["run_id"], "state": "running"}
            if operation == "read-progress":
                return {**self.observation(arguments["run_id"]), "events": "bad"}
            return super().request(
                operation, arguments, operation_id=operation_id
            )

    transport = MalformedTransport()
    cluster = RemoteCluster(host="login.example", root="/cluster/alice/bioimageflow")
    cluster._transport_value = transport

    with pytest.raises(ClusterOperationError) as attach:
        cluster.attach(_RUN_ID)
    assert attach.value.diagnostic.category == "protocol-incompatible"
    assert attach.value.diagnostic.phase == "run-observation"

    transport.malformed_attach = False
    run = cluster.attach(_RUN_ID)
    with pytest.raises(ClusterOperationError) as progress:
        run.progress()
    assert progress.value.diagnostic.category == "protocol-incompatible"
    assert progress.value.diagnostic.phase == "run-progress"


def test_launcher_result_download_errors_are_cluster_errors(tmp_path: Path) -> None:
    class ResultTransport(MockGatewayTransport):
        def download_result(self, _run_id: str, _destination: Path) -> None:
            raise ValueError("malformed launcher transfer receipt")

    transport = ResultTransport()
    cluster = RemoteCluster(host="login.example", root="/cluster/alice/bioimageflow")
    cluster._transport_value = transport
    run = cluster.attach(_RUN_ID)

    with pytest.raises(ClusterOperationError) as raised:
        run.download_result(tmp_path / "result")

    assert raised.value.diagnostic.category == "result-integrity-failed"
    assert raised.value.diagnostic.phase == "result-download"


def test_gateway_emitted_lifecycle_categories_are_normative() -> None:
    assert {"run-not-found", "retry-conflict", "invalid-retry"}.issubset(
        CLUSTER_DIAGNOSTIC_CATEGORIES
    )
