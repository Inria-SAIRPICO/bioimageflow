"""High-level managed remote-cluster lifecycle."""

from __future__ import annotations

import math
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Sequence

from bioimageflow.parsl import ExecutorBinding, ParslTaskPolicy
from bioimageflow.launcher.schemas import new_run_id
from bioimageflow.planning import plan_distributed_execution
from bioimageflow.workflow import Workflow

from ._common import thaw_json
from .deployment import PreparedDeployment, prepare_deployment
from .plan import RemoteExecutionPlan, RemoteNodePlan
from .preparation import PreparedClusterInvocation, prepare_cluster_invocation
from .reports import (
    ClusterCleanupPlan,
    ClusterCleanupReport,
    ClusterConnectionReport,
    ClusterDeployment,
    ClusterDiagnostic,
    ClusterOperationError,
    ClusterValidationReport,
    RemoteSubmissionUncertainError,
)
from .run import RemoteWorkflowRun
from .values import (
    ClusterEnvironment,
    ParslConfiguration,
    RemoteClusterConfig,
    SchedulerJob,
    SetupScript,
)


_RUNTIME_UNVERIFIED = (
    "compute-node shared-root visibility",
    "worker-to-orchestrator networking",
    "nested scheduler submission policy",
    "queue availability at submission time",
    "future quota availability",
    "worker hardware availability",
)


def _utc_after(seconds: float) -> str:
    return (
        datetime.now(timezone.utc) + timedelta(seconds=seconds)
    ).isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse_utc(value: str) -> datetime:
    return datetime.fromisoformat(value[:-1] + "+00:00")


class RemoteCluster:
    """Reusable SSH-accessible managed BioImageFlow execution destination."""

    SCHEMA = RemoteClusterConfig.SCHEMA

    def __init__(
        self,
        *,
        host: str,
        root: str | PurePosixPath,
        environment: ClusterEnvironment | None = None,
        parsl: ParslConfiguration | None = None,
        orchestrator: SchedulerJob | None = None,
        setup: SetupScript | None = None,
        results_root: str | PurePosixPath | None = None,
        connect_timeout: float = 15.0,
    ) -> None:
        self._config = RemoteClusterConfig(
            host=host,
            root=PurePosixPath(root),
            environment=environment,
            parsl=parsl,
            orchestrator=orchestrator,
            setup=setup,
            results_root=(None if results_root is None else PurePosixPath(results_root)),
            connect_timeout=connect_timeout,
        )
        self._transport_value: Any | None = None

    @property
    def host(self) -> str:
        return self._config.host

    @property
    def root(self) -> PurePosixPath:
        return self._config.root

    @property
    def environment(self) -> ClusterEnvironment | None:
        return self._config.environment

    @property
    def parsl(self) -> ParslConfiguration | None:
        return self._config.parsl

    @property
    def orchestrator(self) -> SchedulerJob | None:
        return self._config.orchestrator

    @property
    def setup(self) -> SetupScript | None:
        return self._config.setup

    @property
    def results_root(self) -> PurePosixPath | None:
        return self._config.results_root

    @property
    def connect_timeout(self) -> float:
        return self._config.connect_timeout

    @property
    def configured(self) -> bool:
        return self._config.configured

    def to_dict(self) -> dict[str, Any]:
        return self._config.to_dict()

    @classmethod
    def from_dict(cls, value: Any) -> "RemoteCluster":
        config = RemoteClusterConfig.from_dict(value)
        return cls(
            host=config.host,
            root=config.root,
            environment=config.environment,
            parsl=config.parsl,
            orchestrator=config.orchestrator,
            setup=config.setup,
            results_root=config.results_root,
            connect_timeout=config.connect_timeout,
        )

    def _transport(self) -> Any:
        if self._transport_value is None:
            from .transport import GatewayClientTransport

            self._transport_value = GatewayClientTransport(
                host=self.host,
                root=self.root,
                connect_timeout=self.connect_timeout,
            )
        return self._transport_value

    def _request(
        self,
        operation: str,
        arguments: Mapping[str, Any],
        *,
        operation_id: str | None = None,
    ) -> dict[str, Any]:
        transport = self._transport()
        try:
            return transport.request(
                operation,
                dict(arguments),
                operation_id=operation_id,
            )
        except Exception as exc:
            self._raise_transport_failure(exc, phase=operation)
            raise AssertionError("unreachable")

    @staticmethod
    def _raise_transport_failure(exc: Exception, *, phase: str) -> None:
        from .transport import GatewayTransportError

        if not isinstance(exc, GatewayTransportError):
            raise exc
        raw = exc.diagnostic
        if raw is not None:
            try:
                diagnostic = ClusterDiagnostic.from_dict(raw)
            except (TypeError, ValueError):
                diagnostic = None
            if diagnostic is not None:
                raise ClusterOperationError(diagnostic) from exc
        raise ClusterOperationError(
            ClusterDiagnostic(
                phase=phase.replace("_", "-"),
                category=exc.category,
                message=str(exc),
                allocation_state="unknown" if exc.ambiguous else "none",
                retry_safety="same-attempt-only" if exc.ambiguous else "safe",
                next_action=(
                    "retry-same-attempt"
                    if exc.ambiguous
                    else "inspect-cluster-configuration"
                ),
            )
        ) from exc

    def _require_configuration(self, operation: str) -> None:
        if self.configured:
            return
        raise ClusterOperationError(
            ClusterDiagnostic(
                phase=operation,
                category="configuration-incomplete",
                message=(
                    f"{operation} requires environment, Parsl configuration, "
                    "and orchestrator scheduler settings."
                ),
                retry_safety="safe",
                next_action="complete-cluster-configuration",
            )
        )

    def _require_supported_orchestrator(self, phase: str) -> None:
        assert self.orchestrator is not None
        scheduler = self.orchestrator
        if scheduler.gpu or scheduler.memory is not None or scheduler.attributes:
            raise ClusterOperationError(
                ClusterDiagnostic(
                    phase=phase,
                    category="unsupported-scheduler-adapter",
                    message=(
                        "Managed PSI/J orchestrator jobs do not yet support GPU, "
                        "memory, or custom scheduler resources."
                    ),
                    retry_safety="safe",
                    next_action="remove-unsupported-orchestrator-resources",
                )
            )

    def check_connection(self) -> ClusterConnectionReport:
        result = self._transport().check_connection()
        if type(result) is ClusterConnectionReport:
            return result
        return ClusterConnectionReport.from_dict(result)

    def _publish_prepared_deployment(
        self,
        prepared: PreparedDeployment,
        *,
        progress: Any = None,
    ) -> ClusterDeployment:
        prepared.verify()
        transport = self._transport()
        try:
            if hasattr(transport, "publish_deployment"):
                result = transport.publish_deployment(
                    prepared,
                    progress=progress,
                )
            else:
                result = transport.request(
                    "publish-deployment",
                    {"manifest": prepared.manifest},
                    operation_id=str(uuid.uuid4()),
                )
        except Exception as exc:
            self._raise_transport_failure(exc, phase="deployment")
            raise AssertionError("unreachable")
        if type(result) is ClusterDeployment:
            value = result.to_dict()
        else:
            value = dict(result)
        value.setdefault("schema", ClusterDeployment.SCHEMA)
        value.setdefault("deployment_id", prepared.deployment_id)
        value.setdefault("manifest_digest", prepared.manifest["manifest_digest"])
        assert self.environment is not None
        value.setdefault("ownership", self.environment.ownership)
        value.setdefault("reused", False)
        value.setdefault("environment_kind", self.environment.kind)
        value.setdefault("gateway_publication_id", "gateway-v1")
        value.setdefault("external_attestation_digest", None)
        detached = ClusterDeployment.from_dict(value)
        return ClusterDeployment(
            deployment_id=detached.deployment_id,
            manifest_digest=detached.manifest_digest,
            ownership=detached.ownership,
            reused=detached.reused,
            environment_kind=detached.environment_kind,
            gateway_publication_id=detached.gateway_publication_id,
            external_attestation_digest=detached.external_attestation_digest,
            _cluster=self,
        )

    def deploy(self, *, progress: Any = None) -> ClusterDeployment:
        self._require_configuration("deploy")
        if progress is not None and not callable(progress):
            raise TypeError("progress must be a callable or None.")
        with prepare_deployment(self._config) as prepared:
            return self._publish_prepared_deployment(prepared, progress=progress)

    def prepare(
        self,
        workflow: Workflow,
        *,
        inputs: Mapping[str, Any] | None = None,
        targets: Sequence[str] | None = None,
        node_input_overrides: Mapping[str, Mapping[str, Any]] | None = None,
        task_policy: ParslTaskPolicy | None = None,
        lifetime: float = 3600,
    ) -> PreparedClusterInvocation:
        self._require_configuration("prepare")
        return prepare_cluster_invocation(
            workflow,
            inputs=inputs,
            targets=targets,
            node_input_overrides=node_input_overrides,
            task_policy=task_policy,
            lifetime=lifetime,
        )

    def validate(
        self,
        *,
        deployment: ClusterDeployment,
        timeout: float | None = None,
    ) -> ClusterValidationReport:
        self._require_configuration("validate")
        self._require_supported_orchestrator("validation")
        if type(deployment) is not ClusterDeployment:
            raise TypeError("deployment must be ClusterDeployment.")
        if deployment._cluster not in {None, self}:
            raise ValueError("Deployment belongs to another RemoteCluster.")
        if timeout is not None and (
            type(timeout) not in {int, float}
            or not math.isfinite(float(timeout))
            or timeout <= 0
        ):
            raise ValueError("timeout must be a positive finite number or None.")
        assert self.orchestrator is not None
        result = self._request(
            "validate-deployment",
            {
                "deployment_id": deployment.deployment_id,
                "scheduler_job": self.orchestrator.to_dict(),
                "timeout": timeout,
            },
        )
        return self._validation_report(deployment.deployment_id, result)

    @staticmethod
    def _validation_report(
        deployment_id: str,
        result: Mapping[str, Any],
    ) -> ClusterValidationReport:
        if result.get("schema") == ClusterValidationReport.SCHEMA:
            return ClusterValidationReport.from_dict(result)
        payload = {
            "deployment_id": deployment_id,
            "valid": bool(result.get("valid", False)),
            "expires_at": result.get("expires_at", _utc_after(1800)),
            "executor_bindings": result.get("executor_bindings", {}),
            "verified_facts": result.get("verified_facts", ()),
            "declared_facts": result.get("declared_facts", ()),
            "unverified_facts": result.get("unverified_facts", _RUNTIME_UNVERIFIED),
            "diagnostics": result.get("diagnostics", ()),
            "evidence": result.get("evidence", {}),
        }
        return ClusterValidationReport(
            deployment_id=payload["deployment_id"],
            valid=payload["valid"],
            validation_digest=result.get("validation_digest"),
            expires_at=payload["expires_at"],
            executor_bindings=payload["executor_bindings"],
            verified_facts=tuple(payload["verified_facts"]),
            declared_facts=tuple(payload["declared_facts"]),
            unverified_facts=tuple(payload["unverified_facts"]),
            diagnostics=tuple(
                item if type(item) is ClusterDiagnostic else ClusterDiagnostic.from_dict(item)
                for item in payload["diagnostics"]
            ),
            evidence=payload["evidence"],
        )

    def _remote_storage(self, workflow: Workflow) -> PurePosixPath:
        base = self.results_root or self.root / "results"
        return base / workflow.name

    def plan(
        self,
        prepared: PreparedClusterInvocation,
        *,
        deployment: ClusterDeployment,
        validation: ClusterValidationReport | None = None,
        lifetime: float = 1800,
    ) -> RemoteExecutionPlan:
        self._require_configuration("plan")
        self._require_supported_orchestrator("planning")
        if type(prepared) is not PreparedClusterInvocation:
            raise TypeError("prepared must be PreparedClusterInvocation.")
        if type(deployment) is not ClusterDeployment:
            raise TypeError("deployment must be ClusterDeployment.")
        if validation is not None and type(validation) is not ClusterValidationReport:
            raise TypeError("validation must be ClusterValidationReport or None.")
        if deployment._cluster not in {None, self}:
            raise ValueError("Deployment belongs to another RemoteCluster.")
        if (
            type(lifetime) not in {int, float}
            or not math.isfinite(float(lifetime))
            or lifetime <= 0
        ):
            raise ValueError("lifetime must be a positive finite number of seconds.")
        try:
            prepared._verify()
        except RuntimeError as exc:
            raise ClusterOperationError(
                ClusterDiagnostic(
                    phase="planning",
                    category="local-state-unavailable",
                    message="The prepared invocation bytes are unavailable or changed.",
                    retry_safety="safe",
                    next_action="prepare-again",
                    identities={"invocation_digest": prepared.invocation_digest},
                )
            ) from exc
        report = validation
        if report is None or report.expired:
            report = self.validate(deployment=deployment)
        if report.deployment_id != deployment.deployment_id:
            raise ValueError("Validation report belongs to another deployment.")
        if not report.valid:
            diagnostic = report.diagnostics[0] if report.diagnostics else ClusterDiagnostic(
                phase="planning",
                category="route-incompatible",
                message="Remote validation failed; an execution plan cannot be created.",
                retry_safety="safe",
                next_action="fix-validation-diagnostics",
            )
            raise ClusterOperationError(diagnostic)
        if prepared._workflow is None:
            raise ClusterOperationError(ClusterDiagnostic(phase="planning", category="local-state-unavailable", message="The detached prepared invocation has no owned workflow snapshot.", retry_safety="safe", next_action="prepare-again"))
        bindings = {
            label: ExecutorBinding.from_dict(thaw_json(value))
            for label, value in report.executor_bindings.items()
        }
        if not bindings:
            raise ClusterOperationError(ClusterDiagnostic(phase="planning", category="route-incompatible", message="Validation returned no executor bindings.", retry_safety="safe", next_action="fix-parsl-factory"))
        storage = self._remote_storage(prepared._workflow)
        planning_workflow = prepared._workflow._snapshot_definition(storage_path=str(storage))
        distributed = plan_distributed_execution(
            planning_workflow,
            executor_bindings=bindings,
            task_policy=prepared.manifest.task_policy,
        )
        processing: dict[str, RemoteNodePlan] = {}
        for node in distributed.nodes:
            requirement = {
                "cpu": node.resources.cpu,
                "gpu": node.resources.gpu,
                "memory_bytes": node.resources.memory_bytes,
                "gpu_memory_bytes": node.resources.gpu_memory_bytes,
                "max_concurrent": node.resources.max_concurrent,
            }
            processing[node.scoped_node_path] = RemoteNodePlan(
                node=node.scoped_node_path,
                kind="processing",
                requirement=requirement,
                compatible_executors=node.compatible_executors,
                selected_executor=node.selected_executor,
                route_reason=node.route_reason,
                cache_status=node.execution_status,
                incompatibilities=node.incompatibilities,
                will_dispatch=node.will_dispatch,
                tool_origin=node.tool_origin,
                environment_name=node.environment_name,
                environment_identity=node.environment_identity,
                storage_mode=node.storage_mode,
                diagnostics=node.diagnostics,
            )
        nodes = list(processing.values())
        from bioimageflow.workflow_node import WorkflowNode
        from bioimageflow_core import ProcessingTool

        def add_orchestrator_nodes(workflow: Workflow, prefix: str = "") -> None:
            for name, node in workflow.nodes.items():
                scoped = f"{prefix}/{name}" if prefix else name
                if isinstance(node, WorkflowNode):
                    nodes.append(RemoteNodePlan(node=scoped, kind="workflow", requirement=None, compatible_executors=(), selected_executor=None, route_reason="aggregate", cache_status="aggregate", storage_mode="orchestrator"))
                    add_orchestrator_nodes(node.workflow, scoped)
                elif scoped not in processing and not isinstance(node.tool, ProcessingTool):
                    nodes.append(RemoteNodePlan(node=scoped, kind="dataframe", requirement=None, compatible_executors=(), selected_executor=None, route_reason="orchestrator", cache_status="orchestrator", will_dispatch=True, storage_mode="orchestrator"))

        add_orchestrator_nodes(planning_workflow)
        assert self.orchestrator is not None
        requested_expiry = _utc_after(float(lifetime))
        plan_expiry = min(
            (requested_expiry, report.expires_at),
            key=_parse_utc,
        )
        assert report.validation_digest is not None
        return RemoteExecutionPlan(
            host=self.host,
            cluster_root=str(self.root),
            attempt_id=str(uuid.uuid4()),
            run_id=new_run_id(),
            deployment_id=deployment.deployment_id,
            external_attestation_digest=deployment.external_attestation_digest,
            invocation_digest=prepared.invocation_digest,
            validation_digest=report.validation_digest,
            validation_expires_at=report.expires_at,
            validation_evidence=thaw_json(report.evidence),
            executor_claims=thaw_json(report.executor_bindings),
            storage_path=str(storage),
            expires_at=plan_expiry,
            scheduler_job=self.orchestrator,
            task_policy=prepared.manifest.task_policy,
            nodes=tuple(nodes),
            cluster=self,
            prepared=prepared,
        )

    def _revalidate_plan(self, plan: RemoteExecutionPlan) -> None:
        """Refresh launch-critical claims without mutating the retained attempt."""
        assert self.orchestrator is not None
        result = self._request(
            "validate-deployment",
            {
                "deployment_id": plan.deployment_id,
                "scheduler_job": self.orchestrator.to_dict(),
                "timeout": None,
            },
        )
        report = self._validation_report(plan.deployment_id, result)
        if not report.valid:
            diagnostic = report.diagnostics[0] if report.diagnostics else ClusterDiagnostic(
                phase="submission",
                category="parsl-configuration-changed",
                message="Launch-time deployment validation failed.",
                retry_safety="safe",
                next_action="validate-and-create-new-plan",
                identities={
                    "deployment_id": plan.deployment_id,
                    "run_id": plan.run_id,
                    "attempt_id": plan.attempt_id,
                },
            )
            raise ClusterOperationError(diagnostic)
        fresh_attestation = report.evidence.get("external_attestation_digest")
        claims_changed = (
            thaw_json(report.executor_bindings) != thaw_json(plan.executor_claims)
            or thaw_json(report.evidence) != thaw_json(plan.validation_evidence)
            or fresh_attestation != plan.external_attestation_digest
        )
        if claims_changed:
            raise ClusterOperationError(
                ClusterDiagnostic(
                    phase="submission",
                    category="parsl-configuration-changed",
                    message=(
                        "Launch-time executor, provider, or environment claims differ "
                        "from the confirmed plan."
                    ),
                    retry_safety="safe",
                    next_action="validate-and-create-new-plan",
                    identities={
                        "deployment_id": plan.deployment_id,
                        "run_id": plan.run_id,
                        "attempt_id": plan.attempt_id,
                    },
                )
            )

    def _submit_plan(self, plan: RemoteExecutionPlan) -> RemoteWorkflowRun:
        assert plan._prepared is not None
        transport = self._transport()
        try:
            if hasattr(transport, "submit_plan"):
                result = transport.submit_plan(plan, plan._prepared)
            else:
                result = self._request(
                    "submit-plan",
                    {"plan": plan.to_dict(), "invocation": plan._prepared.to_dict()},
                    operation_id=plan.attempt_id,
                )
        except Exception as exc:
            try:
                self._raise_transport_failure(exc, phase="submission")
            except ClusterOperationError as failure:
                diagnostic = failure.diagnostic
                if (
                    diagnostic.category == "submission-uncertain"
                    or diagnostic.allocation_state == "unknown"
                    or diagnostic.retry_safety == "same-attempt-only"
                ):
                    uncertain = ClusterDiagnostic(
                        phase="submission",
                        category="submission-uncertain",
                        message=diagnostic.message,
                        allocation_state="unknown",
                        retry_safety="same-attempt-only",
                        next_action="attach-run-or-retry-same-plan",
                        identities={
                            **dict(diagnostic.identities),
                            "run_id": plan.run_id,
                            "attempt_id": plan.attempt_id,
                            "deployment_id": plan.deployment_id,
                        },
                    )
                    raise RemoteSubmissionUncertainError(plan, uncertain) from exc
                raise
            raise AssertionError("unreachable")
        run_id = result.get("run_id", plan.run_id)
        if run_id != plan.run_id:
            raise ClusterOperationError(
                ClusterDiagnostic(
                    phase="submission",
                    category="protocol-incompatible",
                    message="The gateway changed the preallocated run ID.",
                    allocation_state="unknown",
                    retry_safety="same-attempt-only",
                    next_action="attach-preallocated-run",
                    identities={
                        "run_id": plan.run_id,
                        "attempt_id": plan.attempt_id,
                    },
                )
            )
        observation = result.get("observation", result)
        try:
            run = RemoteWorkflowRun(self, run_id, observation)
        except (KeyError, TypeError, ValueError) as exc:
            raise ClusterOperationError(
                ClusterDiagnostic(
                    phase="submission",
                    category="protocol-incompatible",
                    message="The gateway returned a malformed initial run observation.",
                    allocation_state="unknown",
                    retry_safety="same-attempt-only",
                    next_action="attach-preallocated-run",
                    identities={"run_id": plan.run_id, "attempt_id": plan.attempt_id},
                )
            ) from exc
        plan._submission_durable()
        return run

    def submit(
        self,
        workflow: Workflow,
        *,
        inputs: Mapping[str, Any] | None = None,
        targets: Sequence[str] | None = None,
        node_input_overrides: Mapping[str, Mapping[str, Any]] | None = None,
        task_policy: ParslTaskPolicy | None = None,
        progress: Any = None,
    ) -> RemoteWorkflowRun:
        self._require_configuration("submit")
        if progress is not None and not callable(progress):
            raise TypeError("progress must be a callable or None.")
        prepared_deployment = prepare_deployment(self._config)
        try:
            prepared_invocation = prepare_cluster_invocation(
                workflow,
                inputs=inputs,
                targets=targets,
                node_input_overrides=node_input_overrides,
                task_policy=task_policy,
            )
        except BaseException:
            prepared_deployment.close()
            raise
        try:
            deployment = self._publish_prepared_deployment(prepared_deployment, progress=progress)
            validation = self.validate(deployment=deployment)
            plan = self.plan(prepared_invocation, deployment=deployment, validation=validation)
            return plan.submit()
        finally:
            prepared_deployment.close()
            if not prepared_invocation.closed and prepared_invocation._lease is None:
                prepared_invocation.close()

    def attach(self, run_id: str) -> RemoteWorkflowRun:
        return RemoteWorkflowRun.open(self, run_id)

    def _download_result(self, run_id: str, destination: Path) -> Any:
        transport = self._transport()
        if hasattr(transport, "download_result"):
            try:
                return transport.download_result(run_id, destination)
            except Exception as exc:
                try:
                    self._raise_transport_failure(exc, phase="result-download")
                except ClusterOperationError:
                    raise
                except Exception as launcher_exc:
                    raise ClusterOperationError(
                        ClusterDiagnostic(
                            phase="result-download",
                            category="result-integrity-failed",
                            message=(
                                "The verified result transfer failed before atomic "
                                "publication."
                            ),
                            retry_safety="safe",
                            next_action="retry-result-download",
                            identities={"run_id": run_id},
                        )
                    ) from launcher_exc
                raise AssertionError("unreachable")
        result = self._request("prepare-result", {"run_id": run_id}, operation_id=str(uuid.uuid4()))
        raise ClusterOperationError(ClusterDiagnostic(phase="result-download", category="local-state-unavailable", message=f"Gateway prepared result transfer {result.get('transfer_id', '<unknown>')}, but this transport cannot download it.", retry_safety="safe", next_action="retry-with-cluster-extra"))

    def plan_cleanup(
        self,
        *,
        namespace: str | None = None,
        run_ids: Sequence[str] = (),
        older_than_seconds: int = 86_400,
    ) -> ClusterCleanupPlan:
        """Inventory exact removable state without mutating the cluster."""
        arguments: dict[str, Any] = {
            "run_ids": list(run_ids),
            "older_than_seconds": older_than_seconds,
        }
        if namespace is not None:
            arguments["namespace"] = namespace
        result = self._request("plan-cleanup", arguments)
        plan = ClusterCleanupPlan.from_dict(result)
        return ClusterCleanupPlan(plan.plan_id, plan.root_revision, plan.candidates, _cluster=self)

    def apply_cleanup(self, plan: ClusterCleanupPlan) -> ClusterCleanupReport:
        """Confirm one cleanup snapshot; changed candidates are skipped."""
        if type(plan) is not ClusterCleanupPlan:
            raise TypeError("plan must be ClusterCleanupPlan.")
        if plan._cluster not in {None, self}:
            raise ValueError("Cleanup plan belongs to another cluster.")
        result = self._request("apply-cleanup", {"plan": plan.to_dict()}, operation_id=plan.plan_id)
        return ClusterCleanupReport.from_dict(result)


__all__ = ["RemoteCluster"]
