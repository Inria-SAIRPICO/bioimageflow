"""Static compilation and routing for one attached Parsl execution."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, get_type_hints

from bioimageflow.engine import DefaultEngine, NodePlan, NodePlanStatus
from bioimageflow.engine.common import _is_shared_array_type
from bioimageflow.node import Node, scoped_node_names
from bioimageflow.worker_origins import resolve_worker_tool_origin
from bioimageflow_core import ProcessingTool
from bioimageflow_core.worker_origins import ArchiveModuleOriginV1

from .materialization import (
    archive_origin_from_source_record,
    materialize_archive_source,
    source_record_by_id,
)
from .requirements import WorkerRequirement, build_worker_requirement
from .routing import RoutingPlan, resolve_executor_routes
from .types import ExecutorBinding


CORE_REQUIREMENT = "bioimageflow-core>=0.3.0,<0.4"


@dataclass(frozen=True, slots=True)
class ParslExecutionPlan:
    """Scheduler plus the exact remote routes needed by one execution."""

    scheduler: DefaultEngine
    routing: RoutingPlan
    plans: tuple[NodePlan, ...]

    @property
    def needs_dfk(self) -> bool:
        return bool(self.routing.routes)


def _reachable_plan(
    scheduler: DefaultEngine,
    targets: list[Node],
    workflow: Any,
) -> tuple[
    tuple[Node, ...],
    dict[Node, str],
    tuple[NodePlan, ...],
]:
    reachable, completion_dependencies, scoped_names = (
        scheduler._compile_execution_graph(targets)
    )
    scheduler._check_env_mismatches(reachable)
    plans: dict[str, NodePlan] = {}
    results: dict[Node, Any] = {}
    reusable_identities: dict[Node, str | None] = {}
    diagnostic_hashes: dict[Node, str] = {}
    with scoped_node_names(scoped_names):
        order = scheduler._topological_sort(
            reachable,
            completion_dependencies,
        )
        scheduler._compiled_ordinals = {
            node: ordinal for ordinal, node in enumerate(order)
        }
        _executable, skipped = scheduler._filter_executable(
            order,
            completion_dependencies,
        )
        for node in order:
            if node in skipped:
                plans[node.name] = NodePlan(
                    node.name,
                    "",
                    NodePlanStatus.SKIPPED,
                    tuple(scheduler._plan_upstream_names(node)),
                )
                continue
            scheduler._plan_node(
                node,
                results,
                reusable_identities,
                diagnostic_hashes,
                workflow,
                plans,
            )
        names = {node: node.name for node in order}
    return tuple(order), names, tuple(plans[names[node]] for node in order)


def _reject_remote_shared_memory(tool: ProcessingTool, node_name: str) -> None:
    for model_name, model in (("input", tool.Inputs), ("output", tool.Outputs)):
        if model is None:
            continue
        declared = model._get_all_annotations()
        try:
            annotations = get_type_hints(model, include_extras=True)
        except (NameError, TypeError):
            annotations = declared
        shared_fields = sorted(
            name
            for name, annotation in annotations.items()
            if _is_shared_array_type(annotation)
        )
        if shared_fields:
            raise TypeError(
                f"Parsl node {node_name!r} has remote {model_name} fields "
                f"{shared_fields} backed by SharedArray."
            )


def _captured_source_records(workflow: Any) -> tuple[dict[str, Any], ...]:
    records = getattr(workflow, "_captured_custom_sources", None)
    if records is None:
        archive = workflow.to_dict(include_custom_tools=True)
        records = archive.get("custom_sources", [])
    return tuple(records)


def _worker_origin(
    node: Node,
    tool: ProcessingTool,
    workflow: Any,
    shared_runtime_root: Path | None,
) -> Any:
    source_id = getattr(type(tool), "_bif_custom_source_id", None)
    if source_id is None:
        return resolve_worker_tool_origin(tool)
    if shared_runtime_root is None:
        raise ValueError(
            f"Archive tool node {node.name!r} requires shared_runtime_root."
        )
    record = source_record_by_id(
        _captured_source_records(workflow),
        source_id,
    )
    return archive_origin_from_source_record(
        record,
        class_name=type(tool).__name__,
        shared_runtime_root=shared_runtime_root,
    )


def _materialize_archives(
    requirements: tuple[WorkerRequirement, ...],
    workflow: Any,
    shared_runtime_root: Path | None,
) -> None:
    archives = {
        requirement.tool_origin.source_id: requirement.tool_origin
        for requirement in requirements
        if isinstance(requirement.tool_origin, ArchiveModuleOriginV1)
    }
    if not archives:
        return
    assert shared_runtime_root is not None
    records = _captured_source_records(workflow)
    for source_id, origin in sorted(archives.items()):
        materialize_archive_source(
            origin,
            source_record_by_id(records, source_id),
            shared_runtime_root=shared_runtime_root,
        )


def prepare_parsl_execution(
    targets: list[Node],
    workflow: Any,
    *,
    executor_bindings: dict[str, ExecutorBinding],
    node_routes: dict[str, str],
    environment_routes: dict[str, str],
    shared_runtime_root: Path | None,
    storage_mode: str,
    sequential: bool,
    cancellation_requested: Callable[[], bool],
) -> ParslExecutionPlan:
    """Compile, plan, route, and materialize before DFK acquisition."""
    validation_errors = workflow.validate(dev_mode=getattr(workflow, "_dev_mode", False))
    if validation_errors:
        raise ValueError(
            "Workflow validation failed before Parsl startup: "
            + "; ".join(str(error) for error in validation_errors)
        )
    scheduler = DefaultEngine(
        force_sequential=sequential,
        cancellation_requested=cancellation_requested,
    )
    order, names, plans = _reachable_plan(scheduler, targets, workflow)
    plans_by_name = {plan.node_name: plan for plan in plans}
    requirements: list[WorkerRequirement] = []
    with scoped_node_names({node: names[node] for node in order}):
        for node in order:
            if not isinstance(node.tool, ProcessingTool):
                continue
            plan = plans_by_name[node.name]
            if plan.status in {NodePlanStatus.CACHED, NodePlanStatus.SKIPPED}:
                continue
            _reject_remote_shared_memory(node.tool, node.name)
            origin = _worker_origin(
                node,
                node.tool,
                workflow,
                shared_runtime_root,
            )
            requirements.append(
                build_worker_requirement(
                    node.name,
                    node.tool,
                    origin,
                    core_requirement=CORE_REQUIREMENT,
                    workflow_environment=workflow._env_configs.get(
                        node.tool.environment.name
                    ),
                    resources=node.effective_resources,
                )
            )
    requirement_tuple = tuple(requirements)
    routing = resolve_executor_routes(
        requirement_tuple,
        executor_bindings=executor_bindings,
        node_routes=node_routes,
        environment_routes=environment_routes,
        storage_mode=storage_mode,
    )
    _materialize_archives(
        requirement_tuple,
        workflow,
        shared_runtime_root,
    )
    return ParslExecutionPlan(
        scheduler=scheduler,
        routing=routing,
        plans=plans,
    )


__all__ = [
    "CORE_REQUIREMENT",
    "ParslExecutionPlan",
    "prepare_parsl_execution",
]
