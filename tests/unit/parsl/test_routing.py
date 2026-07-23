"""Deterministic Parsl executor routing and compatibility validation."""

from __future__ import annotations

from dataclasses import replace

import pytest

from bioimageflow.parsl.requirements import (
    NormalizedResourceRequest,
    WorkerRequirement,
)
from bioimageflow.parsl.routing import (
    ParslRoutingError,
    attestation_environment_identity,
    binding_compatibility_issues,
    resolve_executor_routes,
    validate_executor_labels,
)
from bioimageflow.parsl.types import (
    ExecutorBinding,
    ExecutorCapabilities,
    WorkerEnvironmentAttestation,
    WorkerSlotCapacity,
)
from bioimageflow_core.worker_origins import InstalledModuleOriginV1


CORE_REQUIREMENT = "bioimageflow-core>=0.1.7,<0.2"


def _origin() -> InstalledModuleOriginV1:
    return InstalledModuleOriginV1(
        distribution="example-tools",
        version="1.0.0",
        module="example_tools.worker",
        class_name="ExampleTool",
    )


def _requirement(
    node: str = "workflow/analyze",
    *,
    dependency_hash: str = "a" * 64,
    flexible: bool = False,
    resources: NormalizedResourceRequest | None = None,
) -> WorkerRequirement:
    return WorkerRequirement(
        scoped_node_name=node,
        environment_name="analysis",
        dependency_hash=dependency_hash,
        allow_flexible_versions=flexible,
        core_requirement=CORE_REQUIREMENT,
        anchored_dependency_paths=(),
        resources=resources or NormalizedResourceRequest(),
        tool_origin=_origin(),
    )


def _attestation(
    *,
    dependency_hash: str = "a" * 64,
    flexible: bool = False,
    core_requirement: str = CORE_REQUIREMENT,
) -> WorkerEnvironmentAttestation:
    return WorkerEnvironmentAttestation(
        name="analysis",
        dependency_hash=dependency_hash,
        allow_flexible_versions=flexible,
        core_requirement=core_requirement,
    )


def _binding(
    label: str,
    *,
    attestation: WorkerEnvironmentAttestation | None = None,
    storage_modes: tuple[str, ...] = ("shared_fs",),
    origin_modes: tuple[str, ...] = ("installed_module",),
    slot: WorkerSlotCapacity | None = None,
) -> ExecutorBinding:
    return ExecutorBinding(
        label=label,
        environments=(attestation or _attestation(),),
        capabilities=ExecutorCapabilities(
            storage_modes=storage_modes,
            tool_origin_modes=origin_modes,
            slot=slot
            or WorkerSlotCapacity(
                cpu=4,
                gpu=1,
                memory_bytes=16 * 1024**3,
                gpu_memory_bytes=8 * 1024**3,
            ),
        ),
    )


def test_node_route_has_priority_over_environment_route() -> None:
    requirement = _requirement()
    bindings = {"cpu": _binding("cpu"), "gpu": _binding("gpu")}

    plan = resolve_executor_routes(
        [requirement],
        executor_bindings=bindings,
        node_routes={requirement.scoped_node_name: "cpu"},
        environment_routes={requirement.environment_identity: "gpu"},
    )

    route = plan.route_for_node(requirement.scoped_node_name)
    assert route.executor_label == "cpu"
    assert route.route_source == "node route"


def test_environment_route_is_used_without_node_route() -> None:
    requirement = _requirement()

    plan = resolve_executor_routes(
        [requirement],
        executor_bindings={"cpu": _binding("cpu"), "gpu": _binding("gpu")},
        environment_routes={requirement.environment_identity: "gpu"},
    )

    assert plan.route_for_node(requirement.scoped_node_name).executor_label == "gpu"
    assert (
        plan.route_for_node(requirement.scoped_node_name).route_source
        == "environment route"
    )


def test_unique_compatible_binding_is_selected() -> None:
    requirement = _requirement(
        resources=NormalizedResourceRequest(gpu=1),
    )

    plan = resolve_executor_routes(
        [requirement],
        executor_bindings={
            "cpu": _binding("cpu", slot=WorkerSlotCapacity(cpu=8)),
            "gpu": _binding(
                "gpu",
                slot=WorkerSlotCapacity(cpu=8, gpu=1),
            ),
        },
    )

    route = plan.route_for_node(requirement.scoped_node_name)
    assert route.executor_label == "gpu"
    assert route.route_source == "unique compatible binding"
    assert plan.selected_executor_labels == ("gpu",)


def test_automatic_ambiguity_is_deterministic() -> None:
    bindings = {"zeta": _binding("zeta"), "alpha": _binding("alpha")}

    with pytest.raises(ParslRoutingError) as raised:
        resolve_executor_routes(
            [_requirement()],
            executor_bindings=bindings,
        )

    assert "['alpha', 'zeta']" in str(raised.value)
    assert "configure a node or environment route" in str(raised.value)


def test_explicit_route_never_bypasses_compatibility() -> None:
    requirement = _requirement(
        resources=NormalizedResourceRequest(gpu=1),
    )

    with pytest.raises(ParslRoutingError, match="incompatible executor 'cpu'"):
        resolve_executor_routes(
            [requirement],
            executor_bindings={
                "cpu": _binding("cpu", slot=WorkerSlotCapacity(cpu=8))
            },
            node_routes={requirement.scoped_node_name: "cpu"},
        )


@pytest.mark.parametrize(
    ("binding", "evidence"),
    [
        (
            _binding(
                "candidate",
                attestation=_attestation(dependency_hash="b" * 64),
            ),
            "environment identity",
        ),
        (
            _binding(
                "candidate",
                attestation=_attestation(
                    core_requirement="bioimageflow-core>=0.2,<0.3"
                ),
            ),
            "core requirement",
        ),
        (
            _binding("candidate", storage_modes=("staged",)),
            "storage mode",
        ),
        (
            _binding("candidate", origin_modes=("archive_module",)),
            "tool origin mode",
        ),
        (
            _binding("candidate", slot=WorkerSlotCapacity(cpu=1, gpu=1)),
            "cpu request",
        ),
        (
            _binding("candidate", slot=WorkerSlotCapacity(cpu=4)),
            "gpu request",
        ),
        (
            _binding(
                "candidate",
                slot=WorkerSlotCapacity(cpu=4, gpu=1),
            ),
            "memory is requested",
        ),
        (
            _binding(
                "candidate",
                slot=WorkerSlotCapacity(
                    cpu=4,
                    gpu=1,
                    memory_bytes=4 * 1024**3,
                    gpu_memory_bytes=2 * 1024**3,
                ),
            ),
            "memory request",
        ),
    ],
)
def test_binding_checks_every_declared_compatibility_dimension(
    binding: ExecutorBinding,
    evidence: str,
) -> None:
    requirement = _requirement(
        resources=NormalizedResourceRequest(
            cpu=2,
            gpu=1,
            memory_bytes=8 * 1024**3,
            gpu_memory_bytes=4 * 1024**3,
        )
    )

    assert evidence in " ".join(
        binding_compatibility_issues(requirement, binding)
    )


def test_max_concurrent_is_not_treated_as_slot_capacity() -> None:
    requirement = _requirement(
        resources=NormalizedResourceRequest(max_concurrent=100),
    )

    assert binding_compatibility_issues(
        requirement,
        _binding("cpu", slot=WorkerSlotCapacity(cpu=1)),
    ) == ()


def test_same_name_different_environment_identity_fails_before_routing() -> None:
    first = _requirement("first", dependency_hash="a" * 64)
    second = _requirement("second", dependency_hash="b" * 64)

    with pytest.raises(ParslRoutingError, match="conflicting dependency identities"):
        resolve_executor_routes(
            [second, first],
            executor_bindings={"cpu": _binding("cpu")},
            node_routes={"first": "cpu", "second": "cpu"},
        )


def test_binding_labels_must_exist_in_config_or_dfk() -> None:
    bindings = {"cpu": _binding("cpu"), "gpu": _binding("gpu")}

    with pytest.raises(ParslRoutingError) as raised:
        validate_executor_labels(bindings, {"other", "gpu"})

    assert "['cpu']" in str(raised.value)


def test_route_validation_rejects_unknown_label_and_duplicate_node() -> None:
    requirement = _requirement()

    with pytest.raises(ParslRoutingError, match="unknown executor"):
        resolve_executor_routes(
            [requirement],
            executor_bindings={"cpu": _binding("cpu")},
            node_routes={requirement.scoped_node_name: "missing"},
        )

    with pytest.raises(ParslRoutingError, match="duplicate scoped nodes"):
        resolve_executor_routes(
            [requirement, replace(requirement)],
            executor_bindings={"cpu": _binding("cpu")},
        )


def test_environment_route_requires_canonical_identity_and_shared_fs() -> None:
    requirement = _requirement()

    with pytest.raises(ParslRoutingError, match="canonical environment"):
        resolve_executor_routes(
            [requirement],
            executor_bindings={"cpu": _binding("cpu")},
            environment_routes={"analysis": "cpu"},
        )

    with pytest.raises(ParslRoutingError, match="only storage_mode='shared_fs'"):
        resolve_executor_routes(
            [requirement],
            executor_bindings={"cpu": _binding("cpu")},
            storage_mode="staged",
        )


def test_attestation_identity_matches_requirement_identity() -> None:
    requirement = _requirement()

    assert (
        attestation_environment_identity(_attestation())
        == requirement.environment_identity
    )
