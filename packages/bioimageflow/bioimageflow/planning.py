"""Public deterministic distributed-execution planning."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, ClassVar, Mapping, TYPE_CHECKING

from .integration import IntegrationDiagnostic

if TYPE_CHECKING:
    from .parsl.requirements import NormalizedResourceRequest
    from .parsl.types import ParslTaskPolicy


@dataclass(frozen=True, slots=True)
class DistributedNodePlan:
    scoped_node_path: str
    execution_status: str
    will_dispatch: bool
    resources: "NormalizedResourceRequest"
    compatible_executors: tuple[str, ...]
    selected_executor: str | None
    route_reason: str | None
    tool_origin: str | None
    environment_name: str | None
    environment_identity: str | None
    storage_mode: str
    incompatibilities: Mapping[str, tuple[str, ...]]
    diagnostics: tuple[IntegrationDiagnostic, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "incompatibilities",
            MappingProxyType(
                {
                    label: tuple(reasons)
                    for label, reasons in self.incompatibilities.items()
                }
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "scoped_node_path": self.scoped_node_path,
            "execution_status": self.execution_status,
            "will_dispatch": self.will_dispatch,
            "resources": {
                "cpu": self.resources.cpu,
                "gpu": self.resources.gpu,
                "memory_bytes": self.resources.memory_bytes,
                "gpu_memory_bytes": self.resources.gpu_memory_bytes,
                "max_concurrent": self.resources.max_concurrent,
            },
            "compatible_executors": list(self.compatible_executors),
            "selected_executor": self.selected_executor,
            "route_reason": self.route_reason,
            "tool_origin": self.tool_origin,
            "environment_name": self.environment_name,
            "environment_identity": self.environment_identity,
            "storage_mode": self.storage_mode,
            "incompatibilities": {
                label: list(reasons)
                for label, reasons in self.incompatibilities.items()
            },
            "diagnostics": [item.to_dict() for item in self.diagnostics],
        }

    @classmethod
    def from_dict(cls, value: Any) -> "DistributedNodePlan":
        from .parsl.requirements import NormalizedResourceRequest

        fields = {
            "scoped_node_path",
            "execution_status",
            "will_dispatch",
            "resources",
            "compatible_executors",
            "selected_executor",
            "route_reason",
            "tool_origin",
            "environment_name",
            "environment_identity",
            "storage_mode",
            "incompatibilities",
            "diagnostics",
        }
        if not isinstance(value, dict) or set(value) != fields:
            raise ValueError("Invalid DistributedNodePlan payload.")
        return cls(
            scoped_node_path=value["scoped_node_path"],
            execution_status=value["execution_status"],
            will_dispatch=value["will_dispatch"],
            resources=NormalizedResourceRequest.from_dict(value["resources"]),
            compatible_executors=tuple(value["compatible_executors"]),
            selected_executor=value["selected_executor"],
            route_reason=value["route_reason"],
            tool_origin=value["tool_origin"],
            environment_name=value["environment_name"],
            environment_identity=value["environment_identity"],
            storage_mode=value["storage_mode"],
            incompatibilities={
                label: tuple(reasons)
                for label, reasons in value["incompatibilities"].items()
            },
            diagnostics=tuple(
                IntegrationDiagnostic.from_dict(item)
                for item in value["diagnostics"]
            ),
        )


@dataclass(frozen=True, slots=True)
class DistributedExecutionPlan:
    SCHEMA: ClassVar[str] = "bioimageflow.distributed_execution_plan.v1"
    nodes: tuple[DistributedNodePlan, ...]
    task_policy: "ParslTaskPolicy"
    allocates_resources: bool = False

    @property
    def valid(self) -> bool:
        return all(not node.diagnostics for node in self.nodes)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "allocates_resources": self.allocates_resources,
            "task_policy": self.task_policy.to_dict(),
            "nodes": [node.to_dict() for node in self.nodes],
        }

    @classmethod
    def from_dict(cls, value: Any) -> "DistributedExecutionPlan":
        from .parsl.types import ParslTaskPolicy

        fields = {"schema", "allocates_resources", "task_policy", "nodes"}
        if (
            not isinstance(value, dict)
            or set(value) != fields
            or value["schema"] != cls.SCHEMA
            or value["allocates_resources"] is not False
        ):
            raise ValueError("Invalid DistributedExecutionPlan payload.")
        return cls(
            nodes=tuple(
                DistributedNodePlan.from_dict(item) for item in value["nodes"]
            ),
            task_policy=ParslTaskPolicy.from_dict(value["task_policy"]),
        )


def plan_distributed_execution(
    workflow: Any,
    *,
    executor_bindings: Mapping[str, Any],
    node_routes: Mapping[str, str] | None = None,
    environment_routes: Mapping[str, str] | None = None,
    shared_runtime_root: str | Path | None = None,
    storage_mode: str = "shared_fs",
    task_policy: "ParslTaskPolicy | None" = None,
    targets: list[Any] | None = None,
) -> DistributedExecutionPlan:
    """Plan every reachable ProcessingTool without a DFK or materialization."""
    from bioimageflow_core import ProcessingTool
    from .parsl.requirements import (
        build_worker_requirement,
        normalize_resource_request,
    )
    from .parsl.routing import (
        binding_compatibility_issues,
        resolve_executor_routes,
    )
    from .parsl.startup import (
        CORE_REQUIREMENT,
        _reachable_plan,
        _reject_remote_shared_memory,
        _worker_origin,
    )
    from .parsl.types import ParslTaskPolicy
    from .resources import effective_node_resources

    if task_policy is not None and type(task_policy) is not ParslTaskPolicy:
        raise TypeError("task_policy must be ParslTaskPolicy or None.")
    selected_task_policy = task_policy or ParslTaskPolicy()
    bindings = dict(executor_bindings)
    scheduler_targets = (
        list(workflow._nodes.values())
        if targets is None
        else targets
    )
    from .engine import DefaultEngine, NodePlanStatus

    scheduler = DefaultEngine(use_wetlands=False)
    order, names, node_plans = _reachable_plan(
        scheduler,
        scheduler_targets,
        workflow,
    )
    plans_by_name = {plan.node_name: plan for plan in node_plans}
    resolve_executor_routes(
        (),
        executor_bindings=bindings,
        node_routes=node_routes,
        environment_routes=environment_routes,
        storage_mode=storage_mode,
    )
    root = (
        None
        if shared_runtime_root is None
        else Path(shared_runtime_root).expanduser().resolve(strict=False)
    )
    records: list[DistributedNodePlan] = []
    for node in order:
        if not isinstance(node.tool, ProcessingTool):
            continue
        effective = effective_node_resources(node)
        normalized = normalize_resource_request(effective)
        node_plan = plans_by_name[names[node]]
        will_dispatch = node_plan.status not in {
            NodePlanStatus.CACHED,
            NodePlanStatus.SKIPPED,
        }
        diagnostics: list[IntegrationDiagnostic] = []
        requirement = None
        if will_dispatch:
            try:
                _reject_remote_shared_memory(node.tool, names[node])
                origin = _worker_origin(node, node.tool, workflow, root)
                requirement = build_worker_requirement(
                    names[node],
                    node.tool,
                    origin,
                    core_requirement=CORE_REQUIREMENT,
                    workflow_environment=workflow._env_configs.get(
                        node.tool.environment.name
                    ),
                    resources=effective,
                )
            except Exception as exc:
                diagnostics.append(
                    IntegrationDiagnostic("requirement", str(exc), names[node])
                )
        incompatibilities: dict[str, tuple[str, ...]] = {}
        compatible: tuple[str, ...] = ()
        selected = None
        route_reason = (
            None
            if will_dispatch
            else f"{node_plan.status.value}: no worker dispatch"
        )
        origin_kind = None
        environment_name = node.tool.environment.name
        environment_identity = None
        if requirement is not None:
            origin_kind = requirement.tool_origin_mode
            environment_name = requirement.environment_name
            environment_identity = requirement.environment_identity
            incompatibilities = {
                label: binding_compatibility_issues(
                    requirement,
                    binding,
                    storage_mode=storage_mode,
                )
                for label, binding in sorted(bindings.items())
            }
            compatible = tuple(
                label for label, issues in incompatibilities.items() if not issues
            )
            try:
                route = resolve_executor_routes(
                    (requirement,),
                    executor_bindings=bindings,
                    node_routes=node_routes,
                    environment_routes=environment_routes,
                    storage_mode=storage_mode,
                ).routes[0]
                selected = route.executor_label
                route_reason = route.route_source
            except Exception as exc:
                diagnostics.append(
                    IntegrationDiagnostic("routing", str(exc), names[node])
                )
        records.append(
            DistributedNodePlan(
                scoped_node_path=names[node],
                execution_status=node_plan.status.value,
                will_dispatch=will_dispatch,
                resources=normalized,
                compatible_executors=compatible,
                selected_executor=selected,
                route_reason=route_reason,
                tool_origin=origin_kind,
                environment_name=environment_name,
                environment_identity=environment_identity,
                storage_mode=storage_mode,
                incompatibilities=incompatibilities,
                diagnostics=tuple(diagnostics),
            )
        )
    return DistributedExecutionPlan(
        nodes=tuple(records),
        task_policy=selected_task_policy,
    )


__all__ = [
    "DistributedExecutionPlan",
    "DistributedNodePlan",
    "plan_distributed_execution",
]
