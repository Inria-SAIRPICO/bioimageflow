"""Public submitted-workflow allocation and launcher dispatch."""

from __future__ import annotations

import shutil
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from bioimageflow.parsl import ExecutorBinding, ParslTaskPolicy
from bioimageflow.workflow import Workflow

from .artifacts import write_error
from .configuration import import_config_factory, verify_secret_references
from .errors import BackendNotSupportedError
from .inputs import serialize_invocation
from .payload import serialize_workflow_payload
from .repository import (
    LauncherRepository,
    RunAlreadyExistsError,
)
from .run import WorkflowRun
from .schemas import SUBMISSION_SCHEMA, utc_timestamp
from .types import OrchestratorLaunchConfig, ParslConfigRef


_SUPPORTED_BACKENDS = frozenset({"local", "manual"})


def _normalize_bindings(
    values: Mapping[str, ExecutorBinding],
) -> dict[str, dict[str, Any]]:
    if not isinstance(values, Mapping) or not values:
        raise ValueError("executor_bindings must be a non-empty mapping.")
    result: dict[str, dict[str, Any]] = {}
    for label, binding in values.items():
        if type(label) is not str or not label or label != label.strip():
            raise ValueError(
                "Executor binding labels must be non-empty trimmed strings."
            )
        if type(binding) is not ExecutorBinding:
            raise TypeError(
                "executor_bindings values must be ExecutorBinding instances."
            )
        if binding.label != label:
            raise ValueError(
                f"Executor binding key {label!r} does not match its label."
            )
        result[label] = binding.to_dict()
    return result


def _normalize_routes(
    routes: Mapping[str, str] | None,
    *,
    labels: frozenset[str],
    field: str,
) -> dict[str, str] | None:
    if routes is None:
        return None
    if not isinstance(routes, Mapping):
        raise TypeError(f"{field} must be a mapping or None.")
    result: dict[str, str] = {}
    for route, label in routes.items():
        if type(route) is not str or not route or route != route.strip():
            raise ValueError(f"{field} keys must be non-empty trimmed strings.")
        if type(label) is not str or label not in labels:
            raise ValueError(
                f"{field} route {route!r} names unknown executor {label!r}."
            )
        result[route] = label
    return result


def _absolute_path(
    value: str | Path | None,
    *,
    field: str,
) -> Path | None:
    if value is None:
        return None
    if not isinstance(value, (str, Path)) or (isinstance(value, str) and not value):
        raise TypeError(f"{field} must be a non-empty path-like value or None.")
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    return path.resolve(strict=False)


def _protocol_versions() -> dict[str, int]:
    return {
        "launcher": 1,
        "workflow_graph": 1,
        "workflow_archive": 1,
        "parsl_task": 1,
        "parsl_result": 1,
    }


def _mark_launch_failed(control: Any, error: BaseException) -> None:
    persisted = write_error(
        control,
        code="orchestrator-launch-failed",
        error=error,
    )
    status = control.read_status()
    if status["state"] == "prepared":
        control.transition(
            expected_revision=status["revision"],
            new_state="failed",
            updates={"error": "error.json"},
        )
    del persisted


def submit_workflow(
    workflow: Workflow,
    *,
    inputs: Mapping[str, Any] | None = None,
    targets: Sequence[str] | None = None,
    parsl_config: ParslConfigRef,
    executor_bindings: Mapping[str, ExecutorBinding],
    node_routes: Mapping[str, str] | None = None,
    environment_routes: Mapping[str, str] | None = None,
    shared_runtime_root: Path | str | None = None,
    task_policy: ParslTaskPolicy | None = None,
    launch: OrchestratorLaunchConfig | None = None,
) -> WorkflowRun:
    """Persist and launch one reconnectable submitted Parsl workflow."""
    if not isinstance(workflow, Workflow):
        raise TypeError("workflow must be a Workflow.")
    if type(parsl_config) is not ParslConfigRef:
        raise TypeError("parsl_config must be a ParslConfigRef.")
    selected_launch = (launch or OrchestratorLaunchConfig()).normalized()
    if selected_launch.backend not in _SUPPORTED_BACKENDS:
        raise BackendNotSupportedError(
            f"Launcher backend {selected_launch.backend!r} is not implemented.",
            details={"backend": selected_launch.backend},
        )
    if task_policy is not None and type(task_policy) is not ParslTaskPolicy:
        raise TypeError("task_policy must be a ParslTaskPolicy or None.")

    import_config_factory(parsl_config.factory)
    verify_secret_references(parsl_config)
    payload = serialize_workflow_payload(workflow)
    bindings = _normalize_bindings(executor_bindings)
    labels = frozenset(bindings)
    normalized_node_routes = _normalize_routes(
        node_routes,
        labels=labels,
        field="node_routes",
    )
    normalized_environment_routes = _normalize_routes(
        environment_routes,
        labels=labels,
        field="environment_routes",
    )
    runtime_root = _absolute_path(
        shared_runtime_root,
        field="shared_runtime_root",
    )
    if payload["kind"] == "archive_v1" and runtime_root is None:
        raise ValueError(
            "Submitted workflows with custom sources require "
            "shared_runtime_root."
        )

    storage_root = workflow.storage_path.resolve(strict=False)
    repository = LauncherRepository(storage_root)
    selected_task_policy = task_policy or ParslTaskPolicy()
    last_collision: BaseException | None = None
    for _attempt in range(8):
        run_id = repository.new_run_id()
        candidate = repository.create_candidate(run_id)
        try:
            invocation = serialize_invocation(
                workflow,
                inputs=inputs,
                targets=targets,
                control_candidate=candidate,
            )
            created_at = utc_timestamp()
            submission = {
                "schema": SUBMISSION_SCHEMA,
                "run_id": run_id,
                "created_at": created_at,
                "storage_root": str(storage_root),
                "canonical_view": f"views/runs/{run_id}",
                "workflow": payload,
                "invocation": invocation,
                "parsl_config": parsl_config.to_dict(),
                "executor_bindings": bindings,
                "node_routes": normalized_node_routes,
                "environment_routes": normalized_environment_routes,
                "shared_runtime_root": (
                    None if runtime_root is None else str(runtime_root)
                ),
                "task_policy": selected_task_policy.to_dict(),
                "launch": selected_launch.to_dict(),
                "protocol_versions": _protocol_versions(),
            }
            control = repository.allocate(
                submission,
                backend=selected_launch.backend,
                candidate_dir=candidate,
            )
        except RunAlreadyExistsError as exc:
            last_collision = exc
            if candidate.exists() and not candidate.is_symlink():
                shutil.rmtree(candidate)
            continue
        except BaseException:
            if candidate.exists() and not candidate.is_symlink():
                shutil.rmtree(candidate)
            raise

        try:
            from .backends import launch_orchestrator

            launch_orchestrator(
                control,
                selected_launch,
                secret_refs=tuple((parsl_config.secret_refs or {}).values()),
            )
        except BaseException as exc:
            _mark_launch_failed(control, exc)
            raise
        return WorkflowRun(control)
    raise RuntimeError(
        "Could not allocate a unique submitted workflow run ID."
    ) from last_collision
