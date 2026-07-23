"""Deterministic executor routing and compatibility checks."""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
import re

from .requirements import (
    WorkerRequirement,
    canonical_environment_identity,
    normalize_core_requirement,
)
from .types import ExecutorBinding, WorkerEnvironmentAttestation


class ParslRoutingError(ValueError):
    """Worker requirements cannot be routed without ambiguity or data loss."""


def attestation_environment_identity(
    attestation: WorkerEnvironmentAttestation,
) -> str:
    """Return the route key represented by an executor attestation."""
    if type(attestation) is not WorkerEnvironmentAttestation:
        raise TypeError(
            "attestation must be a WorkerEnvironmentAttestation."
        )
    return canonical_environment_identity(
        name=attestation.name,
        dependency_hash=attestation.dependency_hash,
        allow_flexible_versions=attestation.allow_flexible_versions,
    )


def _validated_bindings(
    bindings: Mapping[str, ExecutorBinding],
) -> dict[str, ExecutorBinding]:
    if not isinstance(bindings, Mapping):
        raise TypeError("executor_bindings must be a mapping.")
    normalized = dict(bindings)
    if not normalized:
        raise ParslRoutingError("executor_bindings must not be empty.")
    for label, binding in sorted(normalized.items(), key=lambda item: repr(item[0])):
        if type(label) is not str or not label or label != label.strip():
            raise ParslRoutingError(
                "Executor binding keys must be non-empty, trimmed strings."
            )
        if type(binding) is not ExecutorBinding:
            raise TypeError(
                f"Executor binding {label!r} must be an ExecutorBinding."
            )
        if binding.label != label:
            raise ParslRoutingError(
                f"Executor binding key {label!r} does not match contained "
                f"label {binding.label!r}."
            )
    return normalized


def validate_executor_labels(
    bindings: Mapping[str, ExecutorBinding],
    available_executor_labels: Collection[str],
) -> None:
    """Require every declared binding label to exist in the Config or DFK."""
    normalized = _validated_bindings(bindings)
    if isinstance(available_executor_labels, (str, bytes)) or not isinstance(
        available_executor_labels, Collection
    ):
        raise TypeError("available_executor_labels must be a collection.")
    if not all(type(label) is str for label in available_executor_labels):
        raise TypeError("available executor labels must be strings.")
    available = frozenset(available_executor_labels)
    missing = sorted(set(normalized) - available)
    if missing:
        raise ParslRoutingError(
            f"Executor bindings name labels absent from the Config or DFK: "
            f"{missing}."
        )


def _matching_attestation(
    requirement: WorkerRequirement,
    binding: ExecutorBinding,
) -> WorkerEnvironmentAttestation | None:
    for attestation in binding.environments:
        if (
            attestation_environment_identity(attestation)
            == requirement.environment_identity
        ):
            return attestation
    return None


def binding_compatibility_issues(
    requirement: WorkerRequirement,
    binding: ExecutorBinding,
    *,
    storage_mode: str = "shared_fs",
) -> tuple[str, ...]:
    """Return stable reasons why one binding cannot run one requirement."""
    if type(requirement) is not WorkerRequirement:
        raise TypeError("requirement must be a WorkerRequirement.")
    if type(binding) is not ExecutorBinding:
        raise TypeError("binding must be an ExecutorBinding.")
    issues: list[str] = []
    capabilities = binding.capabilities
    if storage_mode not in capabilities.storage_modes:
        issues.append(f"storage mode {storage_mode!r} is not attested")

    attestation = _matching_attestation(requirement, binding)
    if attestation is None:
        issues.append(
            f"environment identity {requirement.environment_identity!r} is not attested"
        )
    else:
        try:
            attested_core = normalize_core_requirement(
                attestation.core_requirement
            )
        except (TypeError, ValueError) as exc:
            issues.append(f"core attestation is invalid: {exc}")
        else:
            if attested_core != requirement.core_requirement:
                issues.append(
                    f"core requirement {attested_core!r} does not match "
                    f"{requirement.core_requirement!r}"
                )

    if requirement.tool_origin_mode not in capabilities.tool_origin_modes:
        issues.append(
            f"tool origin mode {requirement.tool_origin_mode!r} is not attested"
        )

    request = requirement.resources
    slot = capabilities.slot
    if request.cpu > slot.cpu:
        issues.append(f"cpu request {request.cpu} exceeds slot capacity {slot.cpu}")
    if request.gpu > slot.gpu:
        issues.append(f"gpu request {request.gpu} exceeds slot capacity {slot.gpu}")
    if request.memory_bytes is not None:
        if slot.memory_bytes is None:
            issues.append("memory is requested but no slot capacity is attested")
        elif request.memory_bytes > slot.memory_bytes:
            issues.append(
                f"memory request {request.memory_bytes} exceeds slot capacity "
                f"{slot.memory_bytes}"
            )
    if request.gpu_memory_bytes is not None:
        if slot.gpu_memory_bytes is None:
            issues.append(
                "gpu memory is requested but no slot capacity is attested"
            )
        elif request.gpu_memory_bytes > slot.gpu_memory_bytes:
            issues.append(
                f"gpu memory request {request.gpu_memory_bytes} exceeds slot "
                f"capacity {slot.gpu_memory_bytes}"
            )
    return tuple(issues)


def _validated_routes(
    routes: Mapping[str, str] | None,
    *,
    field: str,
    binding_labels: frozenset[str],
    canonical_environment_keys: bool = False,
) -> dict[str, str]:
    if routes is None:
        return {}
    if not isinstance(routes, Mapping):
        raise TypeError(f"{field} must be a mapping.")
    normalized = dict(routes)
    for route, label in sorted(normalized.items(), key=lambda item: repr(item[0])):
        if type(route) is not str or not route or route != route.strip():
            raise ParslRoutingError(
                f"{field} keys must be non-empty, trimmed strings."
            )
        if canonical_environment_keys and re.fullmatch(
            r"env_[0-9a-f]{64}",
            route,
        ) is None:
            raise ParslRoutingError(
                f"{field} key {route!r} is not a canonical environment identity."
            )
        if type(label) is not str or label not in binding_labels:
            raise ParslRoutingError(
                f"{field} route {route!r} names unknown executor label "
                f"{label!r}."
            )
    return normalized


def _validate_environment_consistency(
    requirements: Sequence[WorkerRequirement],
) -> None:
    identities_by_name: dict[str, dict[str, list[str]]] = {}
    for requirement in requirements:
        identities_by_name.setdefault(requirement.environment_name, {}).setdefault(
            requirement.environment_identity,
            [],
        ).append(requirement.scoped_node_name)
    for name in sorted(identities_by_name):
        identities = identities_by_name[name]
        if len(identities) <= 1:
            continue
        evidence = [
            f"{identity}: {sorted(nodes)}"
            for identity, nodes in sorted(identities.items())
        ]
        raise ParslRoutingError(
            f"Environment name {name!r} has conflicting dependency identities: "
            f"{evidence}."
        )


def _selected_binding(
    requirement: WorkerRequirement,
    *,
    bindings: Mapping[str, ExecutorBinding],
    node_routes: Mapping[str, str],
    environment_routes: Mapping[str, str],
    storage_mode: str,
) -> tuple[str, str]:
    route_source: str
    explicit_label = node_routes.get(requirement.scoped_node_name)
    if explicit_label is not None:
        selected_label = explicit_label
        route_source = "node route"
    else:
        explicit_label = environment_routes.get(requirement.environment_identity)
        if explicit_label is not None:
            selected_label = explicit_label
            route_source = "environment route"
        else:
            compatible = [
                label
                for label, binding in sorted(bindings.items())
                if not binding_compatibility_issues(
                    requirement,
                    binding,
                    storage_mode=storage_mode,
                )
            ]
            if len(compatible) == 1:
                return compatible[0], "unique compatible binding"
            if len(compatible) > 1:
                raise ParslRoutingError(
                    f"Node {requirement.scoped_node_name!r} has ambiguous "
                    f"compatible executor bindings {compatible}; configure a "
                    "node or environment route."
                )
            evidence = {
                label: binding_compatibility_issues(
                    requirement,
                    binding,
                    storage_mode=storage_mode,
                )
                for label, binding in sorted(bindings.items())
            }
            raise ParslRoutingError(
                f"Node {requirement.scoped_node_name!r} has no compatible "
                f"executor binding; incompatibilities={evidence}."
            )

    issues = binding_compatibility_issues(
        requirement,
        bindings[selected_label],
        storage_mode=storage_mode,
    )
    if issues:
        raise ParslRoutingError(
            f"Explicit {route_source} selects incompatible executor "
            f"{selected_label!r} for node {requirement.scoped_node_name!r}: "
            f"{list(issues)}."
        )
    return selected_label, route_source


@dataclass(frozen=True, slots=True)
class ResolvedWorkerRoute:
    """One requirement assigned to one validated executor binding."""

    requirement: WorkerRequirement
    executor_label: str
    route_source: str


@dataclass(frozen=True, slots=True)
class RoutingPlan:
    """Immutable deterministic output from static executor routing."""

    routes: tuple[ResolvedWorkerRoute, ...]

    @property
    def selected_executor_labels(self) -> tuple[str, ...]:
        return tuple(sorted({route.executor_label for route in self.routes}))

    def route_for_node(self, scoped_node_name: str) -> ResolvedWorkerRoute:
        matches = [
            route
            for route in self.routes
            if route.requirement.scoped_node_name == scoped_node_name
        ]
        if len(matches) != 1:
            raise KeyError(scoped_node_name)
        return matches[0]


def resolve_executor_routes(
    requirements: Sequence[WorkerRequirement],
    *,
    executor_bindings: Mapping[str, ExecutorBinding],
    node_routes: Mapping[str, str] | None = None,
    environment_routes: Mapping[str, str] | None = None,
    available_executor_labels: Collection[str] | None = None,
    storage_mode: str = "shared_fs",
) -> RoutingPlan:
    """Resolve every worker requirement using the normative priority order."""
    if storage_mode != "shared_fs":
        raise ParslRoutingError(
            "Parsl Phase 1a routing supports only storage_mode='shared_fs'."
        )
    if isinstance(requirements, (str, bytes)) or not isinstance(
        requirements, Sequence
    ):
        raise TypeError("requirements must be a sequence.")
    normalized_requirements = tuple(requirements)
    if not all(
        type(requirement) is WorkerRequirement
        for requirement in normalized_requirements
    ):
        raise TypeError("requirements must contain WorkerRequirement values.")
    names = [requirement.scoped_node_name for requirement in normalized_requirements]
    duplicate_names = sorted(
        {name for name in names if names.count(name) > 1}
    )
    if duplicate_names:
        raise ParslRoutingError(
            f"Worker requirements contain duplicate scoped nodes: "
            f"{duplicate_names}."
        )

    bindings = _validated_bindings(executor_bindings)
    if available_executor_labels is not None:
        validate_executor_labels(bindings, available_executor_labels)
    labels = frozenset(bindings)
    normalized_node_routes = _validated_routes(
        node_routes,
        field="node_routes",
        binding_labels=labels,
    )
    normalized_environment_routes = _validated_routes(
        environment_routes,
        field="environment_routes",
        binding_labels=labels,
        canonical_environment_keys=True,
    )
    _validate_environment_consistency(normalized_requirements)

    resolved: list[ResolvedWorkerRoute] = []
    for requirement in normalized_requirements:
        label, source = _selected_binding(
            requirement,
            bindings=bindings,
            node_routes=normalized_node_routes,
            environment_routes=normalized_environment_routes,
            storage_mode=storage_mode,
        )
        resolved.append(
            ResolvedWorkerRoute(
                requirement=requirement,
                executor_label=label,
                route_source=source,
            )
        )
    return RoutingPlan(routes=tuple(resolved))


__all__ = [
    "ParslRoutingError",
    "ResolvedWorkerRoute",
    "RoutingPlan",
    "attestation_environment_identity",
    "binding_compatibility_issues",
    "resolve_executor_routes",
    "validate_executor_labels",
]
