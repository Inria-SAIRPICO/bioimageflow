"""Thin ProcessingBackend adapter for explicit Parsl task submission."""

from __future__ import annotations

from collections.abc import Callable
import threading
from typing import Any

from bioimageflow.backends import ProcessingDispatch
from bioimageflow.engine.output_validation import validate_processing_result_rows
from bioimageflow.parsl.routing import RoutingPlan
from bioimageflow.storage import Storage
from bioimageflow_core import (
    ProcessingTaskV1,
    RowInvocationV1,
    encode_processing_task,
    encode_worker_tool_origin,
)
from bioimageflow_core.worker import execute_processing_task

from .optional_dependency import require_parsl
from .submission import (
    BoundedParslCollector,
    effective_in_flight_limit,
    iter_row_tasks,
    make_batch_task,
)
from .types import ParslTaskPolicy


class PlannedCacheBackend:
    """Fail closed if a planned ProcessingTool cache hit disappears."""

    def prepare_node(self, engine: Any, node: Any, workflow: Any) -> None:
        del engine, node, workflow

    def dispatch(
        self,
        engine: Any,
        request: ProcessingDispatch,
    ) -> list[list[Any]]:
        del engine
        raise RuntimeError(
            f"Planned Parsl cache selection for node {request.node_name!r} "
            "changed before execution; retry the workflow to build a new "
            "attached execution plan."
        )

    def cleanup_execution(self, engine: Any) -> None:
        del engine

    def close(self, engine: Any) -> None:
        del engine


class ParslBackend:
    """Submit resolved ProcessingTool calls without owning orchestration."""

    def __init__(
        self,
        *,
        owner: Any,
        dfk: Any,
        routing: RoutingPlan,
        task_policy: ParslTaskPolicy,
        sequential: bool,
        app_factory: Callable[..., Any] | None = None,
    ) -> None:
        self._owner = owner
        self._dfk = dfk
        self._routing = routing
        self._task_policy = task_policy
        self._sequential = sequential
        self._app_factory = app_factory
        self._apps: dict[str, Callable[..., Any]] = {}
        self._apps_lock = threading.Lock()

    def _app(self, executor_label: str) -> Callable[..., Any]:
        with self._apps_lock:
            app = self._apps.get(executor_label)
            if app is not None:
                return app
            factory = self._app_factory or require_parsl().python_app
            app = factory(
                function=execute_processing_task,
                data_flow_kernel=self._dfk,
                cache=False,
                executors=[executor_label],
            )
            self._apps[executor_label] = app
            return app

    def prepare_node(self, engine: Any, node: Any, workflow: Any) -> None:
        del engine, workflow
        self._routing.route_for_node(node.name)

    def dispatch(
        self,
        engine: Any,
        request: ProcessingDispatch,
    ) -> list[list[Any]]:
        route = self._routing.route_for_node(request.node_name)
        if (
            request.has_batch
            and not request.arguments
            and not getattr(request.tool, "run_empty_batch", False)
        ):
            return []
        rows = tuple(
            RowInvocationV1(
                position=position,
                row_index=request.row_indexes[position],
                arguments=arguments,
                context=request.row_contexts[position].to_dict(),
            )
            for position, arguments in zip(
                request.row_positions,
                request.arguments,
            )
        )
        if request.has_batch:
            tasks: tuple[ProcessingTaskV1, ...] = (
                make_batch_task(
                    node_name=request.node_name,
                    invocation_id=request.invocation_id,
                    cache_attempt_id=request.cache_attempt_id,
                    tool=route.requirement.tool_origin,
                    rows=rows,
                    batch_context=request.batch_context.to_dict(),
                ),
            )
        else:
            tasks = tuple(
                iter_row_tasks(
                    node_name=request.node_name,
                    invocation_id=request.invocation_id,
                    cache_attempt_id=request.cache_attempt_id,
                    tool=route.requirement.tool_origin,
                    rows=rows,
                    row_chunk_size=self._task_policy.row_chunk_size,
                )
            )
        limit = effective_in_flight_limit(
            self._task_policy.max_in_flight,
            route.requirement.resources.max_concurrent,
            sequential=self._sequential,
        )
        app = self._app(route.executor_label)
        storage = Storage(request.workflow.storage_path)

        def task_submitted(task: ProcessingTaskV1) -> None:
            storage.start_backend_task_diagnostic(
                request.run_id,
                request.node_name,
                request.invocation_id,
                task.task_id,
                backend="parsl",
                executor_label=route.executor_label,
                cache_attempt_id=task.cache_attempt_id,
                task_retry=task.task_retry,
                mode=task.mode,
                row_positions=[row.position for row in task.rows],
                tool_origin=encode_worker_tool_origin(task.tool),
            )

        def task_terminal(
            task: ProcessingTaskV1,
            status: str,
            error: BaseException | None,
        ) -> None:
            storage.finish_backend_task_diagnostic(
                request.run_id,
                request.node_name,
                request.invocation_id,
                task.task_id,
                status=status,
                error_type=None if error is None else type(error).__name__,
            )

        collector = BoundedParslCollector(
            submit=lambda task: self._owner._submit_future(
                lambda: app(encode_processing_task(task))
            ),
            max_in_flight=limit,
            node_ordinal=request.compiled_node_ordinal,
            executor_label=route.executor_label,
            cancel_requested=lambda: (
                request.workflow.cancel_requested
                or self._owner.cancel_requested
            ),
            stop_requested=lambda: self._owner.stop_requested,
            task_submitted=task_submitted,
            task_terminal=task_terminal,
            release_future=lambda future, _task: self._owner._release_future(
                future
            ),
            row_complete=lambda position, _index: engine._emit_progress(
                request.workflow,
                request.node_name,
                "row_complete",
                row=position,
                total_rows=len(request.arguments),
            ),
            failure_observed=self._owner._report_task_failure,
            stopped_error=self._owner._submitted_failure_error,
        )
        rows_result = collector.run(tasks)
        assert request.tool.Outputs is not None
        return validate_processing_result_rows(
            rows_result,
            request.tool.Outputs,
            reject_shared_array=True,
        )

    def cleanup_execution(self, engine: Any) -> None:
        del engine

    def close(self, engine: Any) -> None:
        del engine


__all__ = ["ParslBackend", "PlannedCacheBackend"]
