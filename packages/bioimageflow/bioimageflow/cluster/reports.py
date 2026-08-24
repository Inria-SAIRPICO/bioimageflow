"""Strict JSON-safe reports for the managed cluster lifecycle."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import re
from pathlib import PurePosixPath
from types import MappingProxyType
from typing import Any, ClassVar, Literal, Mapping

from ._common import DIGEST_RE, canonical_digest, exact_dict, freeze_json, thaw_json


AllocationState = Literal["none", "orchestrator-submitted", "workers-possible", "unknown"]
RetrySafety = Literal["safe", "same-attempt-only", "unsafe", "not-applicable"]

CLUSTER_DIAGNOSTIC_CATEGORIES = frozenset(
    {
        "ssh-unavailable",
        "ssh-timeout",
        "bootstrap-prerequisite-missing",
        "cluster-root-unsafe",
        "configuration-incomplete",
        "local-state-unavailable",
        "setup-failed",
        "setup-digest-mismatch",
        "environment-lock-invalid",
        "environment-build-lock-incomplete",
        "environment-artifact-missing",
        "environment-installer-unavailable",
        "environment-platform-incompatible",
        "bioimageflow-version-conflict",
        "deployment-install-failed",
        "deployment-tampered",
        "gateway-untrusted",
        "operation-record-tampered",
        "external-environment-changed",
        "secret-reference-missing",
        "parsl-factory-failed",
        "parsl-retries-enabled",
        "executor-label-mismatch",
        "unsupported-scheduler-adapter",
        "unsupported-managed-provider",
        "worker-initialization-missing",
        "route-incompatible",
        "validation-expired",
        "plan-expired",
        "parsl-configuration-changed",
        "scheduler-rejected",
        "submission-uncertain",
        "attempt-still-uncertain",
        "worker-startup-failed",
        "protocol-incompatible",
        "operation-conflict",
        "resource-limit-exceeded",
        "quota-bytes",
        "quota-inodes",
        "insufficient-space",
        "result-integrity-failed",
        "run-not-found",
        "retry-conflict",
        "invalid-retry",
        "cleanup-conflict",
        "gateway-unavailable",
        "remote-operation-failed",
        "unsafe-upload-target",
    }
)

_TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,511}$")


def _nonempty_string(value: Any, *, field_name: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ValueError(f"{field_name} must be a non-empty trimmed string.")
    return value


def _utc_timestamp(value: Any, *, field_name: str) -> str:
    if type(value) is not str or not value.endswith("Z"):
        raise ValueError(f"{field_name} must be a UTC RFC 3339 timestamp ending in Z.")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise ValueError(
            f"{field_name} must be a UTC RFC 3339 timestamp ending in Z."
        ) from exc
    if parsed.tzinfo != timezone.utc:
        raise ValueError(f"{field_name} must be a UTC RFC 3339 timestamp ending in Z.")
    return value


@dataclass(frozen=True, slots=True)
class ClusterDiagnostic:
    """One stable, sanitized managed-cluster diagnostic."""

    SCHEMA: ClassVar[str] = "bioimageflow.cluster_diagnostic.v1"
    phase: str
    category: str
    message: str
    allocation_state: AllocationState = "none"
    retry_safety: RetrySafety = "not-applicable"
    next_action: str = "inspect-configuration"
    identities: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("phase", "category", "message", "next_action"):
            _nonempty_string(getattr(self, name), field_name=name)
        if self.allocation_state not in {"none", "orchestrator-submitted", "workers-possible", "unknown"}:
            raise ValueError("Unknown allocation state.")
        if self.retry_safety not in {"safe", "same-attempt-only", "unsafe", "not-applicable"}:
            raise ValueError("Unknown retry safety.")
        if not isinstance(self.identities, Mapping) or any(
            type(key) is not str or type(value) is not str
            for key, value in self.identities.items()
        ):
            raise TypeError("identities must map strings to strings.")
        object.__setattr__(self, "identities", MappingProxyType(dict(self.identities)))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "phase": self.phase,
            "category": self.category,
            "message": self.message,
            "allocation_state": self.allocation_state,
            "retry_safety": self.retry_safety,
            "next_action": self.next_action,
            "identities": dict(self.identities),
        }

    @classmethod
    def from_dict(cls, value: Any) -> "ClusterDiagnostic":
        data = exact_dict(value, {"schema", "phase", "category", "message", "allocation_state", "retry_safety", "next_action", "identities"}, cls.__name__)
        if data["schema"] != cls.SCHEMA:
            raise ValueError("Unsupported ClusterDiagnostic schema.")
        return cls(**{key: item for key, item in data.items() if key != "schema"})


class ClusterOperationError(RuntimeError):
    """Raised when one managed-cluster operation returns a diagnostic."""

    def __init__(self, diagnostic: ClusterDiagnostic) -> None:
        if type(diagnostic) is not ClusterDiagnostic:
            raise TypeError("diagnostic must be ClusterDiagnostic.")
        self.diagnostic = diagnostic
        super().__init__(f"{diagnostic.category}: {diagnostic.message}")


class RemoteSubmissionUncertainError(ClusterOperationError):
    """A recoverable managed submission whose allocation outcome is unknown."""

    def __init__(self, plan: Any, diagnostic: ClusterDiagnostic) -> None:
        if diagnostic.category != "submission-uncertain":
            raise ValueError(
                "RemoteSubmissionUncertainError requires submission-uncertain."
            )
        self.plan = plan
        self.host = plan.host
        self.root = plan.cluster_root
        self.cluster_root = plan.cluster_root
        self.run_id = plan.run_id
        self.attempt_id = plan.attempt_id
        self.category = diagnostic.category
        self.next_action = diagnostic.next_action
        super().__init__(diagnostic)


@dataclass(frozen=True, slots=True)
class ClusterConnectionReport:
    SCHEMA: ClassVar[str] = "bioimageflow.cluster_connection_report.v1"
    reachable: bool
    gateway_available: bool
    bootstrap_required: bool
    gateway_version: str | None = None
    protocol_versions: tuple[int, ...] = ()
    diagnostics: tuple[ClusterDiagnostic, ...] = ()

    def __post_init__(self) -> None:
        if any(type(value) is not bool for value in (self.reachable, self.gateway_available, self.bootstrap_required)):
            raise TypeError("Connection state fields must be booleans.")
        if self.gateway_available and (not self.reachable or self.bootstrap_required):
            raise ValueError("An available gateway must be reachable and need no bootstrap.")
        if self.gateway_version is not None:
            _nonempty_string(self.gateway_version, field_name="gateway_version")
        versions = tuple(self.protocol_versions)
        if any(type(item) is not int or item <= 0 for item in versions) or len(set(versions)) != len(versions):
            raise ValueError("protocol_versions must contain distinct positive integers.")
        diagnostics = tuple(self.diagnostics)
        if any(type(item) is not ClusterDiagnostic for item in diagnostics):
            raise TypeError("diagnostics must contain ClusterDiagnostic values.")
        object.__setattr__(self, "protocol_versions", versions)
        object.__setattr__(self, "diagnostics", diagnostics)

    @property
    def valid(self) -> bool:
        return self.reachable and not self.diagnostics

    def to_dict(self) -> dict[str, Any]:
        return {"schema": self.SCHEMA, "reachable": self.reachable, "gateway_available": self.gateway_available, "bootstrap_required": self.bootstrap_required, "gateway_version": self.gateway_version, "protocol_versions": list(self.protocol_versions), "diagnostics": [item.to_dict() for item in self.diagnostics]}

    @classmethod
    def from_dict(cls, value: Any) -> "ClusterConnectionReport":
        data = exact_dict(value, {"schema", "reachable", "gateway_available", "bootstrap_required", "gateway_version", "protocol_versions", "diagnostics"}, cls.__name__)
        if data["schema"] != cls.SCHEMA:
            raise ValueError("Unsupported ClusterConnectionReport schema.")
        return cls(data["reachable"], data["gateway_available"], data["bootstrap_required"], data["gateway_version"], tuple(data["protocol_versions"]), tuple(ClusterDiagnostic.from_dict(item) for item in data["diagnostics"]))


@dataclass(frozen=True, slots=True)
class ClusterDeployment:
    SCHEMA: ClassVar[str] = "bioimageflow.cluster_deployment.v1"
    deployment_id: str
    manifest_digest: str
    ownership: Literal["content", "external"]
    reused: bool
    environment_kind: str
    gateway_publication_id: str
    external_attestation_digest: str | None = None
    _cluster: Any = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        for name in ("deployment_id", "manifest_digest"):
            value = getattr(self, name)
            if type(value) is not str or DIGEST_RE.fullmatch(value) is None:
                raise ValueError(f"{name} must be a SHA-256 digest.")
        if self.external_attestation_digest is not None and DIGEST_RE.fullmatch(self.external_attestation_digest) is None:
            raise ValueError("external_attestation_digest must be a SHA-256 digest.")
        if self.ownership not in {"content", "external"}:
            raise ValueError("ownership must be content or external.")
        if type(self.reused) is not bool:
            raise TypeError("reused must be boolean.")
        _nonempty_string(self.environment_kind, field_name="environment_kind")
        _nonempty_string(self.gateway_publication_id, field_name="gateway_publication_id")

    def to_dict(self) -> dict[str, Any]:
        return {"schema": self.SCHEMA, "deployment_id": self.deployment_id, "manifest_digest": self.manifest_digest, "ownership": self.ownership, "reused": self.reused, "environment_kind": self.environment_kind, "gateway_publication_id": self.gateway_publication_id, "external_attestation_digest": self.external_attestation_digest}

    @classmethod
    def from_dict(cls, value: Any) -> "ClusterDeployment":
        data = exact_dict(value, {"schema", "deployment_id", "manifest_digest", "ownership", "reused", "environment_kind", "gateway_publication_id", "external_attestation_digest"}, cls.__name__)
        if data["schema"] != cls.SCHEMA:
            raise ValueError("Unsupported ClusterDeployment schema.")
        return cls(**{key: item for key, item in data.items() if key != "schema"})


@dataclass(frozen=True, slots=True)
class ClusterValidationReport:
    SCHEMA: ClassVar[str] = "bioimageflow.cluster_validation_report.v1"
    deployment_id: str
    valid: bool
    validation_digest: str | None
    expires_at: str
    executor_bindings: Mapping[str, Any] = field(default_factory=dict)
    verified_facts: tuple[str, ...] = ()
    declared_facts: tuple[str, ...] = ()
    unverified_facts: tuple[str, ...] = ()
    diagnostics: tuple[ClusterDiagnostic, ...] = ()
    evidence: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if type(self.deployment_id) is not str or DIGEST_RE.fullmatch(self.deployment_id) is None:
            raise ValueError("deployment_id must be a SHA-256 digest.")
        if type(self.valid) is not bool:
            raise TypeError("valid must be boolean.")
        _utc_timestamp(self.expires_at, field_name="expires_at")
        for name in ("verified_facts", "declared_facts", "unverified_facts"):
            values = tuple(getattr(self, name))
            if any(type(item) is not str or not item for item in values) or len(set(values)) != len(values):
                raise ValueError(f"{name} must contain distinct non-empty strings.")
            object.__setattr__(self, name, values)
        diagnostics = tuple(self.diagnostics)
        if any(type(item) is not ClusterDiagnostic for item in diagnostics):
            raise TypeError("diagnostics must contain ClusterDiagnostic values.")
        object.__setattr__(self, "diagnostics", diagnostics)
        bindings = freeze_json(self.executor_bindings, path="executor_bindings", reject_sensitive_keys=False)
        if not isinstance(bindings, Mapping):
            raise TypeError("executor_bindings must be a mapping.")
        evidence = freeze_json(self.evidence, path="evidence", reject_sensitive_keys=False)
        if not isinstance(evidence, Mapping):
            raise TypeError("evidence must be a mapping.")
        object.__setattr__(self, "executor_bindings", bindings)
        object.__setattr__(self, "evidence", evidence)
        computed = canonical_digest(self._payload(include_digest=False))
        if self.validation_digest is None:
            object.__setattr__(self, "validation_digest", computed)
        elif (
            type(self.validation_digest) is not str
            or DIGEST_RE.fullmatch(self.validation_digest) is None
            or self.validation_digest != computed
        ):
            raise ValueError("Cluster validation report digest mismatch.")

    @property
    def expired(self) -> bool:
        expiry = datetime.fromisoformat(self.expires_at[:-1] + "+00:00")
        return datetime.now(timezone.utc) >= expiry

    def _payload(self, *, include_digest: bool) -> dict[str, Any]:
        payload = {
            "schema": self.SCHEMA,
            "deployment_id": self.deployment_id,
            "valid": self.valid,
            "expires_at": self.expires_at,
            "executor_bindings": thaw_json(self.executor_bindings),
            "verified_facts": list(self.verified_facts),
            "declared_facts": list(self.declared_facts),
            "unverified_facts": list(self.unverified_facts),
            "diagnostics": [item.to_dict() for item in self.diagnostics],
            "evidence": thaw_json(self.evidence),
        }
        if include_digest:
            payload["validation_digest"] = self.validation_digest
        return payload

    def to_dict(self) -> dict[str, Any]:
        if canonical_digest(self._payload(include_digest=False)) != self.validation_digest:
            raise ValueError("Cluster validation report digest mismatch.")
        return self._payload(include_digest=True)

    @classmethod
    def from_dict(cls, value: Any) -> "ClusterValidationReport":
        data = exact_dict(value, {"schema", "deployment_id", "valid", "validation_digest", "expires_at", "executor_bindings", "verified_facts", "declared_facts", "unverified_facts", "diagnostics", "evidence"}, cls.__name__)
        if data["schema"] != cls.SCHEMA:
            raise ValueError("Unsupported ClusterValidationReport schema.")
        return cls(deployment_id=data["deployment_id"], valid=data["valid"], validation_digest=data["validation_digest"], expires_at=data["expires_at"], executor_bindings=data["executor_bindings"], verified_facts=tuple(data["verified_facts"]), declared_facts=tuple(data["declared_facts"]), unverified_facts=tuple(data["unverified_facts"]), diagnostics=tuple(ClusterDiagnostic.from_dict(item) for item in data["diagnostics"]), evidence=data["evidence"])


@dataclass(frozen=True, slots=True)
class ClusterCleanupCandidate:
    namespace: str
    identity: str
    path: str
    size: int
    reference_reasons: tuple[str, ...] = ()
    consequences: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name in ("namespace", "identity"):
            value = _nonempty_string(getattr(self, name), field_name=name)
            if _TOKEN_RE.fullmatch(value) is None:
                raise ValueError(f"{name} contains unsupported characters.")
        _nonempty_string(self.path, field_name="path")
        logical = PurePosixPath(self.path)
        if (
            logical.is_absolute()
            or str(logical) != self.path
            or "\\" in self.path
            or any(part in {"", ".", ".."} for part in logical.parts)
        ):
            raise ValueError("path must be a normalized relative POSIX path.")
        if type(self.size) is not int or self.size < 0:
            raise ValueError("size must be a non-negative integer.")
        references = tuple(self.reference_reasons)
        consequences = tuple(self.consequences)
        if any(type(item) is not str or not item for item in references):
            raise ValueError("reference_reasons must contain non-empty strings.")
        if any(type(item) is not str or not item for item in consequences):
            raise ValueError("consequences must contain non-empty strings.")
        object.__setattr__(self, "reference_reasons", references)
        object.__setattr__(self, "consequences", consequences)

    def to_dict(self) -> dict[str, Any]:
        return {"namespace": self.namespace, "identity": self.identity, "path": self.path, "size": self.size, "reference_reasons": list(self.reference_reasons), "consequences": list(self.consequences)}

    @classmethod
    def from_dict(cls, value: Any) -> "ClusterCleanupCandidate":
        data = exact_dict(value, {"namespace", "identity", "path", "size", "reference_reasons", "consequences"}, cls.__name__)
        return cls(data["namespace"], data["identity"], data["path"], data["size"], tuple(data["reference_reasons"]), tuple(data["consequences"]))


@dataclass(frozen=True, slots=True)
class ClusterCleanupPlan:
    SCHEMA: ClassVar[str] = "bioimageflow.cluster_cleanup_plan.v1"
    plan_id: str
    root_revision: int
    candidates: tuple[ClusterCleanupCandidate, ...]
    _cluster: Any = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        _nonempty_string(self.plan_id, field_name="plan_id")
        if type(self.root_revision) is not int or self.root_revision < 0:
            raise ValueError("root_revision must be a non-negative integer.")
        candidates = tuple(self.candidates)
        if any(type(item) is not ClusterCleanupCandidate for item in candidates):
            raise TypeError("candidates must contain ClusterCleanupCandidate values.")
        object.__setattr__(self, "candidates", candidates)

    def to_dict(self) -> dict[str, Any]:
        return {"schema": self.SCHEMA, "plan_id": self.plan_id, "root_revision": self.root_revision, "candidates": [item.to_dict() for item in self.candidates]}

    @classmethod
    def from_dict(cls, value: Any) -> "ClusterCleanupPlan":
        data = exact_dict(value, {"schema", "plan_id", "root_revision", "candidates"}, cls.__name__)
        if data["schema"] != cls.SCHEMA:
            raise ValueError("Unsupported ClusterCleanupPlan schema.")
        return cls(data["plan_id"], data["root_revision"], tuple(ClusterCleanupCandidate.from_dict(item) for item in data["candidates"]))


@dataclass(frozen=True, slots=True)
class ClusterCleanupReport:
    SCHEMA: ClassVar[str] = "bioimageflow.cluster_cleanup_report.v1"
    plan_id: str
    removed: tuple[str, ...]
    skipped: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _nonempty_string(self.plan_id, field_name="plan_id")
        removed = tuple(self.removed)
        if any(type(item) is not str or not item for item in removed):
            raise ValueError("removed must contain non-empty identity strings.")
        if not isinstance(self.skipped, Mapping) or any(
            type(key) is not str or not key or type(value) is not str or not value
            for key, value in self.skipped.items()
        ):
            raise TypeError("skipped must map non-empty identity and reason strings.")
        object.__setattr__(self, "removed", removed)
        object.__setattr__(self, "skipped", MappingProxyType(dict(self.skipped)))

    def to_dict(self) -> dict[str, Any]:
        return {"schema": self.SCHEMA, "plan_id": self.plan_id, "removed": list(self.removed), "skipped": dict(self.skipped)}

    @classmethod
    def from_dict(cls, value: Any) -> "ClusterCleanupReport":
        data = exact_dict(value, {"schema", "plan_id", "removed", "skipped"}, cls.__name__)
        if data["schema"] != cls.SCHEMA:
            raise ValueError("Unsupported ClusterCleanupReport schema.")
        return cls(data["plan_id"], tuple(data["removed"]), data["skipped"])


__all__ = [
    "CLUSTER_DIAGNOSTIC_CATEGORIES",
    "ClusterCleanupCandidate",
    "ClusterCleanupPlan",
    "ClusterCleanupReport",
    "ClusterConnectionReport",
    "ClusterDeployment",
    "ClusterDiagnostic",
    "ClusterOperationError",
    "ClusterValidationReport",
    "RemoteSubmissionUncertainError",
]
