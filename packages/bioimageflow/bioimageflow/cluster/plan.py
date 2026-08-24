"""Confirmed remote execution plans and their local ownership lease."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Any, ClassVar, Mapping

from bioimageflow.integration import IntegrationDiagnostic
from bioimageflow.launcher.schemas import validate_run_id
from bioimageflow.parsl import ParslTaskPolicy

from ._common import (
    DIGEST_RE,
    canonical_digest,
    exact_dict,
    freeze_json,
    normalized_cluster_path,
    thaw_json,
    validate_host,
)
from .preparation import PreparedClusterInvocation
from .reports import ClusterDiagnostic, ClusterOperationError
from .values import SchedulerJob


@dataclass(frozen=True, slots=True)
class RemoteNodePlan:
    """One scoped node in a non-allocating remote plan."""

    node: str
    kind: str
    requirement: Mapping[str, Any] | None
    compatible_executors: tuple[str, ...]
    selected_executor: str | None
    route_reason: str | None
    cache_status: str
    incompatibilities: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    will_dispatch: bool = False
    tool_origin: str | None = None
    environment_name: str | None = None
    environment_identity: str | None = None
    storage_mode: str | None = None
    diagnostics: tuple[IntegrationDiagnostic, ...] = ()

    def __post_init__(self) -> None:
        if type(self.node) is not str or not self.node:
            raise ValueError("node must be a non-empty scoped path.")
        if self.kind not in {"processing", "dataframe", "workflow"}:
            raise ValueError("Unknown remote node kind.")
        if type(self.will_dispatch) is not bool:
            raise TypeError("will_dispatch must be boolean.")
        if self.requirement is not None:
            if not isinstance(self.requirement, Mapping):
                raise TypeError("requirement must be a mapping or None.")
            object.__setattr__(self, "requirement", MappingProxyType(dict(self.requirement)))
        object.__setattr__(self, "incompatibilities", MappingProxyType({label: tuple(reasons) for label, reasons in self.incompatibilities.items()}))
        diagnostics = tuple(self.diagnostics)
        if any(type(item) is not IntegrationDiagnostic for item in diagnostics):
            raise TypeError("diagnostics must contain IntegrationDiagnostic values.")
        object.__setattr__(self, "diagnostics", diagnostics)

    def to_dict(self) -> dict[str, Any]:
        return {
            "node": self.node,
            "kind": self.kind,
            "requirement": None if self.requirement is None else dict(self.requirement),
            "compatible_executors": list(self.compatible_executors),
            "selected_executor": self.selected_executor,
            "route_reason": self.route_reason,
            "cache_status": self.cache_status,
            "incompatibilities": {label: list(reasons) for label, reasons in self.incompatibilities.items()},
            "will_dispatch": self.will_dispatch,
            "tool_origin": self.tool_origin,
            "environment_name": self.environment_name,
            "environment_identity": self.environment_identity,
            "storage_mode": self.storage_mode,
            "diagnostics": [item.to_dict() for item in self.diagnostics],
        }

    @classmethod
    def from_dict(cls, value: Any) -> "RemoteNodePlan":
        data = exact_dict(value, {"node", "kind", "requirement", "compatible_executors", "selected_executor", "route_reason", "cache_status", "incompatibilities", "will_dispatch", "tool_origin", "environment_name", "environment_identity", "storage_mode", "diagnostics"}, cls.__name__)
        return cls(
            node=data["node"],
            kind=data["kind"],
            requirement=data["requirement"],
            compatible_executors=tuple(data["compatible_executors"]),
            selected_executor=data["selected_executor"],
            route_reason=data["route_reason"],
            cache_status=data["cache_status"],
            incompatibilities={label: tuple(reasons) for label, reasons in data["incompatibilities"].items()},
            will_dispatch=data["will_dispatch"],
            tool_origin=data["tool_origin"],
            environment_name=data["environment_name"],
            environment_identity=data["environment_identity"],
            storage_mode=data["storage_mode"],
            diagnostics=tuple(
                IntegrationDiagnostic.from_dict(item) for item in data["diagnostics"]
            ),
        )


class RemoteExecutionPlan:
    """One immutable, idempotent remote submission attempt."""

    SCHEMA: ClassVar[str] = "bioimageflow.remote_execution_plan.v1"
    _IMMUTABLE_FIELDS: ClassVar[frozenset[str]] = frozenset(
        {
            "host",
            "cluster_root",
            "attempt_id",
            "run_id",
            "deployment_id",
            "external_attestation_digest",
            "invocation_digest",
            "validation_digest",
            "validation_expires_at",
            "validation_evidence",
            "executor_claims",
            "storage_path",
            "expires_at",
            "scheduler_job",
            "task_policy",
            "nodes",
            "plan_digest",
        }
    )

    def __setattr__(self, name: str, value: Any) -> None:
        if name in self._IMMUTABLE_FIELDS and hasattr(self, name):
            raise AttributeError("Remote execution plan confirmation fields are immutable.")
        super().__setattr__(name, value)

    def __init__(
        self,
        *,
        host: str,
        cluster_root: str,
        attempt_id: str,
        run_id: str,
        deployment_id: str,
        external_attestation_digest: str | None,
        invocation_digest: str,
        validation_digest: str,
        validation_expires_at: str,
        validation_evidence: Mapping[str, Any],
        executor_claims: Mapping[str, Any],
        storage_path: str,
        expires_at: str,
        scheduler_job: SchedulerJob,
        task_policy: ParslTaskPolicy,
        nodes: tuple[RemoteNodePlan, ...],
        plan_digest: str | None = None,
        cluster: Any = None,
        prepared: PreparedClusterInvocation | None = None,
    ) -> None:
        self.host = validate_host(host)
        self.cluster_root = str(
            normalized_cluster_path(cluster_root, field="cluster_root")
        )
        self.attempt_id = self._uuid4(attempt_id, field="attempt_id")
        self.run_id = validate_run_id(run_id)
        self.deployment_id = deployment_id
        self.external_attestation_digest = external_attestation_digest
        self.invocation_digest = invocation_digest
        self.validation_digest = validation_digest
        self.validation_expires_at = validation_expires_at
        self.validation_evidence = freeze_json(
            validation_evidence,
            path="validation_evidence",
            reject_sensitive_keys=False,
        )
        self.executor_claims = freeze_json(
            executor_claims,
            path="executor_claims",
            reject_sensitive_keys=False,
        )
        self.storage_path = str(
            normalized_cluster_path(storage_path, field="storage_path")
        )
        self.expires_at = expires_at
        self.scheduler_job = scheduler_job
        self.task_policy = task_policy
        self.nodes = tuple(nodes)
        self._cluster = cluster
        self._prepared = prepared
        self._closed = False
        self._sealed = False
        self._run: Any = None
        self._lease_id = self.attempt_id
        for name in ("deployment_id", "invocation_digest", "validation_digest"):
            if type(getattr(self, name)) is not str or DIGEST_RE.fullmatch(getattr(self, name)) is None:
                raise ValueError(f"{name} must be a SHA-256 digest.")
        if self.external_attestation_digest is not None and DIGEST_RE.fullmatch(self.external_attestation_digest) is None:
            raise ValueError("external_attestation_digest must be a SHA-256 digest.")
        if type(scheduler_job) is not SchedulerJob or type(task_policy) is not ParslTaskPolicy:
            raise TypeError("scheduler_job and task_policy have invalid types.")
        if not isinstance(self.validation_evidence, Mapping):
            raise TypeError("validation_evidence must be a mapping.")
        if not isinstance(self.executor_claims, Mapping):
            raise TypeError("executor_claims must be a mapping.")
        if any(type(node) is not RemoteNodePlan for node in self.nodes):
            raise TypeError("nodes must contain only RemoteNodePlan values.")
        self._parse_expiry()
        self._parse_timestamp(
            self.validation_expires_at,
            field="validation_expires_at",
        )
        computed = canonical_digest(self._payload(include_digest=False))
        if plan_digest is not None and plan_digest != computed:
            raise ValueError("Remote execution plan digest mismatch.")
        self.plan_digest = computed
        if prepared is not None:
            prepared._acquire_lease(self._lease_id)

    @staticmethod
    def _uuid4(value: object, *, field: str) -> str:
        if type(value) is not str:
            raise TypeError(f"{field} must be a canonical UUID4 string.")
        try:
            parsed = uuid.UUID(value)
        except ValueError as exc:
            raise ValueError(f"{field} must be a canonical UUID4 string.") from exc
        if parsed.version != 4 or str(parsed) != value:
            raise ValueError(f"{field} must be a canonical UUID4 string.")
        return value

    def _parse_expiry(self) -> datetime:
        return self._parse_timestamp(self.expires_at, field="expires_at")

    @staticmethod
    def _parse_timestamp(value: Any, *, field: str) -> datetime:
        if type(value) is not str or not value.endswith("Z"):
            raise ValueError(f"{field} must be a UTC RFC 3339 timestamp ending in Z.")
        try:
            expiry = datetime.fromisoformat(value[:-1] + "+00:00")
        except ValueError as exc:
            raise ValueError(
                f"{field} must be a UTC RFC 3339 timestamp ending in Z."
            ) from exc
        if expiry.tzinfo != timezone.utc:
            raise ValueError(f"{field} must be a UTC RFC 3339 timestamp ending in Z.")
        return expiry

    def _verify_digest(self) -> None:
        if canonical_digest(self._payload(include_digest=False)) != self.plan_digest:
            raise ClusterOperationError(
                ClusterDiagnostic(
                    phase="submission",
                    category="protocol-incompatible",
                    message="The remote execution plan digest no longer matches.",
                    retry_safety="safe",
                    next_action="create-new-plan",
                    identities={
                        "run_id": self.run_id,
                        "attempt_id": self.attempt_id,
                    },
                )
            )

    @property
    def detached(self) -> bool:
        return self._cluster is None or self._prepared is None

    @property
    def expired(self) -> bool:
        return datetime.now(timezone.utc) >= self._parse_expiry()

    @property
    def validation_expired(self) -> bool:
        return datetime.now(timezone.utc) >= self._parse_timestamp(
            self.validation_expires_at,
            field="validation_expires_at",
        )

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def valid(self) -> bool:
        return all(not node.diagnostics for node in self.nodes)

    def _payload(self, *, include_digest: bool) -> dict[str, Any]:
        payload = {
            "schema": self.SCHEMA,
            "host": self.host,
            "cluster_root": self.cluster_root,
            "attempt_id": self.attempt_id,
            "run_id": self.run_id,
            "deployment_id": self.deployment_id,
            "external_attestation_digest": self.external_attestation_digest,
            "invocation_digest": self.invocation_digest,
            "validation_digest": self.validation_digest,
            "validation_expires_at": self.validation_expires_at,
            "validation_evidence": thaw_json(self.validation_evidence),
            "executor_claims": thaw_json(self.executor_claims),
            "storage_path": self.storage_path,
            "expires_at": self.expires_at,
            "scheduler_job": self.scheduler_job.to_dict(),
            "task_policy": self.task_policy.to_dict(),
            "nodes": [node.to_dict() for node in self.nodes],
        }
        if include_digest:
            payload["plan_digest"] = self.plan_digest
        return payload

    def to_dict(self) -> dict[str, Any]:
        self._verify_digest()
        return self._payload(include_digest=True)

    @classmethod
    def from_dict(cls, value: Any) -> "RemoteExecutionPlan":
        fields = {"schema", "host", "cluster_root", "attempt_id", "run_id", "deployment_id", "external_attestation_digest", "invocation_digest", "validation_digest", "validation_expires_at", "validation_evidence", "executor_claims", "storage_path", "expires_at", "scheduler_job", "task_policy", "nodes", "plan_digest"}
        data = exact_dict(value, fields, cls.__name__)
        if data["schema"] != cls.SCHEMA:
            raise ValueError("Unsupported RemoteExecutionPlan schema.")
        return cls(
            host=data["host"],
            cluster_root=data["cluster_root"],
            attempt_id=data["attempt_id"],
            run_id=data["run_id"],
            deployment_id=data["deployment_id"],
            external_attestation_digest=data["external_attestation_digest"],
            invocation_digest=data["invocation_digest"],
            validation_digest=data["validation_digest"],
            validation_expires_at=data["validation_expires_at"],
            validation_evidence=data["validation_evidence"],
            executor_claims=data["executor_claims"],
            storage_path=data["storage_path"],
            expires_at=data["expires_at"],
            scheduler_job=SchedulerJob.from_dict(data["scheduler_job"]),
            task_policy=ParslTaskPolicy.from_dict(data["task_policy"]),
            nodes=tuple(RemoteNodePlan.from_dict(item) for item in data["nodes"]),
            plan_digest=data["plan_digest"],
        )

    def submit(self) -> Any:
        if self._run is not None:
            return self._run
        if self._closed:
            raise ClusterOperationError(
                ClusterDiagnostic(
                    phase="submission",
                    category="local-state-unavailable",
                    message="The remote execution plan is closed.",
                    retry_safety="safe",
                    next_action="create-new-plan",
                    identities={
                        "run_id": self.run_id,
                        "attempt_id": self.attempt_id,
                    },
                )
            )
        if self.expired:
            raise ClusterOperationError(
                ClusterDiagnostic(
                    phase="submission",
                    category="plan-expired",
                    message="The confirmed remote execution plan has expired.",
                    retry_safety="safe",
                    next_action="create-new-plan",
                    identities={"run_id": self.run_id, "attempt_id": self.attempt_id},
                )
            )
        if self.validation_expired:
            raise ClusterOperationError(
                ClusterDiagnostic(
                    phase="submission",
                    category="validation-expired",
                    message="The validation bound to this plan has expired.",
                    retry_safety="safe",
                    next_action="validate-and-create-new-plan",
                    identities={
                        "deployment_id": self.deployment_id,
                        "run_id": self.run_id,
                        "attempt_id": self.attempt_id,
                    },
                )
            )
        if self.detached:
            raise ClusterOperationError(
                ClusterDiagnostic(
                    phase="submission",
                    category="local-state-unavailable",
                    message="The detached plan does not own its prepared invocation bytes.",
                    retry_safety="safe",
                    next_action="attach-run-or-prepare-again",
                    identities={"run_id": self.run_id, "attempt_id": self.attempt_id},
                )
            )
        if not self.valid:
            failing = next(node for node in self.nodes if node.diagnostics)
            issue = failing.diagnostics[0]
            raise ClusterOperationError(
                ClusterDiagnostic(
                    phase="submission",
                    category="route-incompatible",
                    message=issue.message,
                    retry_safety="safe",
                    next_action="fix-node-routing",
                    identities={"node": failing.node, "run_id": self.run_id},
                )
            )
        self._verify_digest()
        assert self._cluster is not None
        assert self._prepared is not None
        try:
            self._prepared._verify()
        except RuntimeError as exc:
            raise ClusterOperationError(
                ClusterDiagnostic(
                    phase="submission",
                    category="local-state-unavailable",
                    message="The prepared invocation bytes are unavailable or changed.",
                    retry_safety="safe",
                    next_action="prepare-and-create-new-plan",
                    identities={
                        "invocation_digest": self.invocation_digest,
                        "run_id": self.run_id,
                        "attempt_id": self.attempt_id,
                    },
                )
            ) from exc
        self._cluster._revalidate_plan(self)
        self._sealed = True
        self._run = self._cluster._submit_plan(self)
        return self._run

    def close(self) -> None:
        if self._closed:
            return
        if self._sealed:
            raise ClusterOperationError(
                ClusterDiagnostic(
                    phase="submission",
                    category="attempt-still-uncertain",
                    message=(
                        "The sealed plan cannot release its snapshot until the gateway "
                        "confirms durable submission or pre-submission cancellation."
                    ),
                    allocation_state="unknown",
                    retry_safety="same-attempt-only",
                    next_action="attach-run-or-retry-same-plan",
                    identities={"run_id": self.run_id, "attempt_id": self.attempt_id},
                )
            )
        self._closed = True
        if self._prepared is not None:
            self._prepared._release_lease(self._lease_id)

    def _submission_durable(self) -> None:
        if self._prepared is not None:
            self._prepared._release_lease(self._lease_id)
        self._closed = True

    def __enter__(self) -> "RemoteExecutionPlan":
        if self._closed:
            raise ClusterOperationError(
                ClusterDiagnostic(
                    phase="planning",
                    category="local-state-unavailable",
                    message="The remote execution plan is closed.",
                    retry_safety="safe",
                    next_action="create-new-plan",
                    identities={
                        "run_id": self.run_id,
                        "attempt_id": self.attempt_id,
                    },
                )
            )
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if exc_type is not None and self._sealed and not self._closed:
            # Preserve the original submission diagnostic while retaining the
            # prepared-byte lease for same-attempt recovery.
            return
        self.close()


__all__ = ["RemoteExecutionPlan", "RemoteNodePlan"]
