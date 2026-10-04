"""Focused methods extracted from the execution engine."""

# Pyright checks the complete contract on DefaultEngine; this module contains one partial mixin.
# pyright: reportAttributeAccessIssue=false

from __future__ import annotations

import threading
import uuid
from dataclasses import replace
from typing import cast

from .shared_arrays import SharedTaskScope

from bioimageflow_core import (
    ConsumedRow,
    OutputGroup,
    ReferenceRow,
    ProcessingTask,
    ResourceSpec,
    RowInvocation,
    decode_processing_result,
    encode_processing_task,
    validate_processing_result,
)
from .output_validation import (
    normalize_processing_batch_outputs,
    normalize_processing_row_outputs,
    validate_processing_result_rows,
)

from .common import (
    Any,
    Arguments,
    ExecutionContext,
    ProcessingDispatch,
    ProcessingTool,
    WorkerTimeoutError,
    WorkflowCancelledError,
    _accepts_context,
    _compute_engine_timeout,
    _raise_worker_task_error,
)


class _WetlandsTaskTracker:
    """Cancel and drain the Wetlands tasks owned by one dispatch."""

    def __init__(self, workflow: Any) -> None:
        self._lock = threading.Lock()
        self._tasks: list[Any] = []
        self._cancel_requested = False
        context = getattr(workflow, "_active_run_context", None)
        if context is None:
            self._unsubscribe = lambda: None
        else:
            self._unsubscribe = context._subscribe_cancellation(self.request_cancel)

    def register(self, tasks: list[Any]) -> None:
        """Track a submitted window, cancelling it if cancellation already won."""
        with self._lock:
            self._tasks.extend(tasks)
            cancel_requested = self._cancel_requested
        if cancel_requested:
            self._cancel_unfinished(tasks)

    def request_cancel(self) -> None:
        """Request cooperative cancellation of every unfinished task."""
        with self._lock:
            self._cancel_requested = True
            tasks = tuple(self._tasks)
        self._cancel_unfinished(tasks)

    def cancel_and_drain(self) -> None:
        """Cancel unfinished work and wait until every submitted task is terminal."""
        self.request_cancel()
        with self._lock:
            tasks = tuple(self._tasks)
        for task in tasks:
            while not task.state.terminal:
                try:
                    task.wait_for()
                except BaseException:
                    continue

    def close(self) -> None:
        """Detach the cancellation observer after all dispatch work is settled."""
        self._unsubscribe()

    @staticmethod
    def _cancel_unfinished(tasks: tuple[Any, ...] | list[Any]) -> None:
        for task in tasks:
            if not task.state.terminal:
                try:
                    task.cancel()
                except BaseException:
                    pass


class _DispatchMixin:
    def _dispatch_tool(
        self,
        tool: ProcessingTool,
        arguments_dicts: list[dict[str, Any]],
        workflow: Any,
        node_name: str,
        row_contexts: list[ExecutionContext],
        batch_context: ExecutionContext,
        *,
        invocation_id: str,
        cache_attempt_id: str | None,
    ) -> list[OutputGroup]:
        """Dispatch to process_batch or process_row. Returns list[list[Outputs]]."""
        has_batch = type(tool).process_batch is not ProcessingTool.process_batch
        compiled_node, compiled_node_ordinal = next(
            (node, ordinal)
            for node, ordinal in self._compiled_ordinals.items()
            if node.name == node_name
        )
        row_indexes = tuple(
            context.row_index if context.row_index is not None else str(position)
            for position, context in enumerate(row_contexts)
        )
        run_view = getattr(workflow, "_run_view_context", None)
        if not isinstance(run_view, dict) or "run_id" not in run_view:
            raise RuntimeError("Processing dispatch requires an active workflow run.")

        request = ProcessingDispatch(
            tool=tool,
            arguments=tuple(arguments_dicts),
            workflow=workflow,
            node_name=node_name,
            row_contexts=tuple(row_contexts),
            batch_context=batch_context,
            has_batch=has_batch,
            invocation_id=invocation_id,
            cache_attempt_id=cache_attempt_id,
            run_id=str(run_view["run_id"]),
            run_context=getattr(workflow, "_active_run_context", None),
            compiled_node_ordinal=compiled_node_ordinal,
            row_positions=tuple(range(len(arguments_dicts))),
            row_indexes=row_indexes,
            resources=compiled_node.effective_resources,
        )
        return self._backend.dispatch(self, request)

    def _dispatch_direct(
        self, tool: ProcessingTool, arguments_dicts: list[dict[str, Any]],
        workflow: Any, node_name: str, has_batch: bool,
        row_contexts: list[ExecutionContext], batch_context: ExecutionContext,
    ) -> list[OutputGroup]:
        payload_values = [arguments_dicts, vars(batch_context.batch_arguments) if batch_context.batch_arguments is not None else {}, [row.arguments for row in batch_context.reference_rows]]
        scope = SharedTaskScope(workflow.shared_memory_context, uuid.uuid4().hex, payload_values)
        borrowed = scope.borrowed()
        try:
            with borrowed.activate():
                arguments, batch_values, reference_values = borrowed.bind_value(payload_values)
                bound_context = replace(batch_context, batch_arguments=Arguments(**batch_values), reference_rows=tuple(ReferenceRow(row.position, row.row_index, values) for row, values in zip(batch_context.reference_rows, reference_values, strict=True)))
                outputs = self._dispatch_direct_bound(
                    tool, arguments, workflow, node_name, has_batch, row_contexts, bound_context,
                )
            bound = scope.accept_outputs([list(group.outputs) for group in outputs])
            return [replace(group, outputs=tuple({field: getattr(output, field) for field in output._get_all_annotations()} for output in values)) for group, values in zip(outputs, bound, strict=True)]
        except BaseException:
            scope.fail()
            raise
        finally:
            scope.drained()

    def _dispatch_direct_bound(
        self,
        tool: ProcessingTool,
        arguments_dicts: list[dict[str, Any]],
        workflow: Any,
        node_name: str,
        has_batch: bool,
        row_contexts: list[ExecutionContext],
        batch_context: ExecutionContext,
    ) -> list[OutputGroup]:
        """Direct dispatch — tool runs in the main process."""
        if has_batch:
            if not arguments_dicts and tool.row_consumption.value == "mapped":
                return []
            args_list = [Arguments(**d) for d in arguments_dicts]
            kwargs = {}
            if _accepts_context(tool.process_batch):
                kwargs["context"] = batch_context
            raw_results = tool.process_batch(args_list, **kwargs)
            assert tool.Outputs is not None
            normalized = normalize_processing_batch_outputs(
                raw_results,
                tool.Outputs,
                expected_rows=len(arguments_dicts),
                row_consumption=tool.row_consumption.value,
            )
            consumed = tuple(ConsumedRow(position, str(context.row_index)) for position, context in enumerate(row_contexts))
            if tool.row_consumption.value == "collective":
                return [OutputGroup(consumed, cast(Any, tuple(normalized[0])))]
            return [OutputGroup((row,), cast(Any, tuple(outputs))) for row, outputs in zip(consumed, normalized, strict=True)]

        raw_results: list[OutputGroup] = []
        accepts_context = _accepts_context(tool.process_row)
        for i, (args_dict, context) in enumerate(zip(arguments_dicts, row_contexts)):
            kwargs = {"context": context} if accepts_context else {}
            result = tool.process_row(Arguments(**args_dict), **kwargs)
            assert tool.Outputs is not None
            raw_results.append(OutputGroup((ConsumedRow(i, str(context.row_index)),), cast(Any, tuple(normalize_processing_row_outputs(result, tool.Outputs)))))
            self._emit_progress(
                workflow,
                node_name,
                "row_complete",
                row=i,
                total_rows=len(arguments_dicts),
            )
        return raw_results

    def _resolve_worker_config(
        self,
        tool: ProcessingTool,
        workflow: Any,
    ) -> tuple[int, float | None]:
        """Determine Wetlands 2 pool size and worker health timeout."""
        env_name = tool.environment.name
        env_config = workflow._env_configs.get(env_name)

        # max_workers: explicit override > workflow default
        if env_config and env_config.max_workers > 0:
            max_workers = env_config.max_workers
        else:
            max_workers = workflow.max_workers

        worker_timeout = env_config.worker_timeout if env_config else None

        return max_workers, worker_timeout

    def _dispatch_via_wetlands(
        self,
        tool: ProcessingTool,
        arguments_dicts: list[dict[str, Any]],
        workflow: Any,
        node_name: str,
        has_batch: bool,
        row_contexts: list[ExecutionContext],
        batch_context: ExecutionContext,
        invocation_id: str,
        cache_attempt_id: str | None,
        resources: ResourceSpec | None = None,
    ) -> list[OutputGroup]:
        """Dispatch through Wetlands — tool runs in isolated environment workers."""
        from bioimageflow.worker_origins import resolve_worker_tool_origin
        from wetlands import ExecutionEventKind, ExecutionState

        assert self._env_manager is not None
        if (
            has_batch
            and not arguments_dicts
            and tool.row_consumption.value == "mapped"
        ):
            return []
        env_spec = tool.environment
        origin = resolve_worker_tool_origin(tool)
        max_workers, worker_timeout = self._resolve_worker_config(tool, workflow)
        engine_timeout = _compute_engine_timeout(worker_timeout)
        tracker = _WetlandsTaskTracker(workflow)
        scopes: list[SharedTaskScope] = []
        handed_off: set[int] = set()

        try:
            if has_batch:
                if workflow.cancel_requested:
                    raise WorkflowCancelledError("Workflow cancelled by user")
                invocation = ProcessingTask(
                    task_id="task_0000000000000000",
                    node_name=node_name,
                    invocation_id=invocation_id,
                    cache_attempt_id=cache_attempt_id,
                    task_retry=0,
                    mode="process_batch",
                    row_consumption=tool.row_consumption.value,
                    tool=origin,
                    rows=tuple(
                        RowInvocation(
                            position=position,
                            row_index=(
                                context.row_index
                                if context.row_index is not None
                                else str(position)
                            ),
                            arguments=arguments,
                            context=context.to_dict(),
                        )
                        for position, (arguments, context) in enumerate(
                            zip(arguments_dicts, row_contexts)
                        )
                    ),
                    batch_context=batch_context.to_dict(),
                    batch_arguments=vars(batch_context.batch_arguments) if batch_context.batch_arguments is not None else {},
                    reference_rows=batch_context.reference_rows,
                )
                scope = SharedTaskScope(workflow.shared_memory_context, invocation_id, [arguments_dicts, invocation.batch_arguments, [row.arguments for row in invocation.reference_rows]])
                scopes.append(scope)
                invocation = replace(invocation, shared_memory_context=scope.wire)
                payload = encode_processing_task(invocation)
                handed_off.add(id(scope))
                task = self._env_manager.submit_processing_task(
                    env_spec,
                    payload,
                    max_workers=max_workers,
                    worker_timeout=worker_timeout,
                    shared_memory_grant=scope,
                )
                tracker.register([task])
                if workflow.cancel_requested:
                    raise WorkflowCancelledError("Workflow cancelled by user")
                try:
                    task.wait_for(timeout=engine_timeout)
                except TimeoutError:
                    if workflow.cancel_requested:
                        raise WorkflowCancelledError("Workflow cancelled by user")
                    self._emit_progress(workflow, node_name, "failed")
                    raise WorkerTimeoutError(
                        f"Batch task for node '{node_name}' exceeded engine-side "
                        f"timeout ({engine_timeout:.0f}s; "
                        f"worker_timeout={worker_timeout}s)"
                    )
                except Exception:
                    if workflow.cancel_requested:
                        raise WorkflowCancelledError("Workflow cancelled by user")
                    _raise_worker_task_error(
                        task,
                        node_name=node_name,
                        tool=tool,
                        row_index=None,
                    )
                if workflow.cancel_requested or task.state == ExecutionState.CANCELED:
                    raise WorkflowCancelledError(
                        "Workflow cancelled during batch execution"
                    )
                if task.state == ExecutionState.FAILED:
                    _raise_worker_task_error(
                        task,
                        node_name=node_name,
                        tool=tool,
                        row_index=None,
                    )
                result = decode_processing_result(task.result)
                validate_processing_result(invocation, result)
                assert tool.Outputs is not None
                validated = validate_processing_result_rows(result.groups, tool.Outputs)
                bound = scope.accept_outputs(validated)
                return [replace(group, outputs=tuple({field: getattr(output, field) for field in output._get_all_annotations()} for output in values)) for group, values in zip(result.groups, bound, strict=True)]

            invocations = [
                ProcessingTask(
                    task_id=f"task_{position:016x}",
                    node_name=node_name,
                    invocation_id=invocation_id,
                    cache_attempt_id=cache_attempt_id,
                    task_retry=0,
                    mode="row_chunk",
                    row_consumption=tool.row_consumption.value,
                    tool=origin,
                    rows=(
                        RowInvocation(
                            position=position,
                            row_index=(
                                context.row_index
                                if context.row_index is not None
                                else str(position)
                            ),
                            arguments=arguments,
                            context=context.to_dict(),
                        ),
                    ),
                )
                for position, (arguments, context) in enumerate(
                    zip(arguments_dicts, row_contexts)
                )
            ]
            for index, invocation in enumerate(invocations):
                scope = SharedTaskScope(
                    workflow.shared_memory_context,
                    invocation_id + invocation.task_id,
                    [row.arguments for row in invocation.rows],
                )
                scopes.append(scope)
                invocations[index] = replace(invocation, shared_memory_context=scope.wire)
            payloads = [encode_processing_task(invocation) for invocation in invocations]
            selected_resources = (
                resources or getattr(tool, "resources", None) or ResourceSpec()
            )
            window = selected_resources.max_concurrent or len(payloads) or 1
            tasks: list[Any] = []
            for start in range(0, len(payloads), window):
                if workflow.cancel_requested:
                    raise WorkflowCancelledError("Workflow cancelled by user")
                active: list[Any] = []
                for payload in payloads[start : start + window]:
                    if workflow.cancel_requested:
                        raise WorkflowCancelledError("Workflow cancelled by user")
                    handed_off.add(id(scopes[len(tasks)]))
                    task = self._env_manager.submit_processing_task(
                        env_spec,
                        payload,
                        max_workers=max_workers,
                        worker_timeout=worker_timeout,
                        shared_memory_grant=scopes[len(tasks)],
                    )
                    active.append(task)
                    tasks.append(task)
                    tracker.register([task])
                for offset, task in enumerate(active):
                    row_position = start + offset

                    def _make_listener(row_idx):
                        def on_event(event):
                            if event.kind == ExecutionEventKind.UPDATE:
                                self._emit_progress(
                                    workflow,
                                    node_name,
                                    "row_progress",
                                    row=row_idx,
                                    total_rows=len(payloads),
                                    message=event.message,
                                    current=event.current,
                                    maximum=event.maximum,
                                )

                        return on_event

                    task.listen(_make_listener(row_position))
                for offset, task in enumerate(active):
                    row_position = start + offset
                    if workflow.cancel_requested:
                        raise WorkflowCancelledError("Workflow cancelled by user")
                    try:
                        task.wait_for(timeout=engine_timeout)
                    except TimeoutError:
                        if workflow.cancel_requested:
                            raise WorkflowCancelledError("Workflow cancelled by user")
                        self._emit_progress(
                            workflow,
                            node_name,
                            "failed",
                            row=row_position,
                            total_rows=len(payloads),
                        )
                        raise WorkerTimeoutError(
                            f"Task for node '{node_name}' row {row_position} "
                            f"exceeded engine-side timeout ({engine_timeout:.0f}s; "
                            f"worker_timeout={worker_timeout}s)"
                        )
                    except Exception:
                        if workflow.cancel_requested:
                            raise WorkflowCancelledError("Workflow cancelled by user")
                        _raise_worker_task_error(
                            task,
                            node_name=node_name,
                            tool=tool,
                            row_index=row_contexts[row_position].row_index,
                        )
                    if task.state == ExecutionState.FAILED:
                        _raise_worker_task_error(
                            task,
                            node_name=node_name,
                            tool=tool,
                            row_index=row_contexts[row_position].row_index,
                        )
                    if workflow.cancel_requested:
                        raise WorkflowCancelledError("Workflow cancelled by user")

            # Collect results in submission order only while cancellation has not won.
            raw_results: list[OutputGroup] = []
            assert tool.Outputs is not None
            for i, task in enumerate(tasks):
                if workflow.cancel_requested or task.state == ExecutionState.CANCELED:
                    raise WorkflowCancelledError("Workflow cancelled by user")
                result = decode_processing_result(task.result)
                validate_processing_result(invocations[i], result)
                row_result = result.groups[0]
                values = scopes[i].accept_outputs(validate_processing_result_rows((row_result,), tool.Outputs))[0]
                raw_results.append(replace(row_result, outputs=tuple({field: getattr(output, field) for field in output._get_all_annotations()} for output in values)))
                self._emit_progress(
                    workflow, node_name, "row_complete", row=i, total_rows=len(tasks)
                )
            return raw_results
        except BaseException:
            for scope in scopes:
                scope.fail()
            cancelled = workflow.cancel_requested
            tracker.cancel_and_drain()
            if cancelled:
                raise WorkflowCancelledError("Workflow cancelled by user") from None
            raise
        finally:
            for scope in scopes:
                if id(scope) not in handed_off:
                    scope.drained()
            tracker.close()
