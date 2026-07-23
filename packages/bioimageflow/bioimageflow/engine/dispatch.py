"""Focused methods extracted from the execution engine."""

# Pyright checks the complete contract on DefaultEngine; this module contains one partial mixin.
# pyright: reportAttributeAccessIssue=false

from __future__ import annotations

import uuid

from bioimageflow_core import (
    ProcessingTaskV1,
    RowInvocationV1,
    decode_processing_result,
    encode_processing_task,
    validate_processing_result,
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


class _DispatchMixin:
    def _dispatch_tool(
        self,
        tool: ProcessingTool,
        arguments_dicts: list[dict[str, Any]],
        workflow: Any,
        node_name: str,
        row_contexts: list[ExecutionContext],
        batch_context: ExecutionContext,
    ) -> list[list[Any]]:
        """Dispatch to process_batch or process_row. Returns list[list[Outputs]]."""
        has_batch = type(tool).process_batch is not ProcessingTool.process_batch

        request = ProcessingDispatch(
            tool=tool,
            arguments=tuple(arguments_dicts),
            workflow=workflow,
            node_name=node_name,
            row_contexts=tuple(row_contexts),
            batch_context=batch_context,
            has_batch=has_batch,
        )
        return self._backend.dispatch(self, request)

    def _dispatch_direct(
        self,
        tool: ProcessingTool,
        arguments_dicts: list[dict[str, Any]],
        workflow: Any,
        node_name: str,
        has_batch: bool,
        row_contexts: list[ExecutionContext],
        batch_context: ExecutionContext,
    ) -> list[list[Any]]:
        """Direct dispatch — tool runs in the main process."""
        if has_batch:
            if not arguments_dicts and not getattr(tool, "run_empty_batch", False):
                return []
            args_list = [Arguments(**d) for d in arguments_dicts]
            kwargs = {}
            if _accepts_context(tool.process_batch):
                kwargs["context"] = batch_context
            raw_results = tool.process_batch(args_list, **kwargs)
            if raw_results and not isinstance(raw_results[0], list):
                raw_results = [[r] for r in raw_results]
            return raw_results

        raw_results: list[list[Any]] = []
        accepts_context = _accepts_context(tool.process_row)
        for i, (args_dict, context) in enumerate(zip(arguments_dicts, row_contexts)):
            kwargs = {"context": context} if accepts_context else {}
            result = tool.process_row(Arguments(**args_dict), **kwargs)
            if not isinstance(result, list):
                result = [result]
            raw_results.append(result)
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
    ) -> tuple[int, Any, float | None]:
        """Determine max_workers, worker_env, and worker_timeout for a tool's environment.

        Resolution order:
        1. Explicit ``get_environment()`` override takes precedence.
        2. GPU auto-inference: if any tool in the environment declares
           ``ResourceSpec(gpu >= 1)`` and no explicit ``worker_env`` was set,
           auto-generate ``worker_env = lambda i: {"CUDA_VISIBLE_DEVICES": str(i)}``.
        3. Fall back to ``Workflow.max_workers``, no ``worker_env``, no ``worker_timeout``.
        """
        env_name = tool.environment.name
        env_config = workflow._env_configs.get(env_name)

        # max_workers: explicit override > workflow default
        if env_config and env_config.max_workers > 0:
            max_workers = env_config.max_workers
        else:
            max_workers = workflow.max_workers

        # worker_env: explicit override > GPU auto-inference > None
        if env_config and env_config.worker_env is not None:
            worker_env = env_config.worker_env
        elif self._env_has_gpu_tool(env_name, workflow):

            def wef(i):
                return {"CUDA_VISIBLE_DEVICES": str(i)}

            worker_env = wef
        else:
            worker_env = None

        worker_timeout = env_config.worker_timeout if env_config else None

        return max_workers, worker_env, worker_timeout

    def _env_has_gpu_tool(self, env_name: str, workflow: Any) -> bool:
        """Check if any tool in this workflow sharing this env declares gpu >= 1."""
        for node in workflow._nodes.values():
            tool = node.tool
            if (
                isinstance(tool, ProcessingTool)
                and hasattr(tool, "environment")
                and tool.environment.name == env_name
                and hasattr(tool, "resources")
                and tool.resources is not None
                and tool.resources.gpu >= 1
            ):
                return True
        return False

    def _dispatch_via_wetlands(
        self,
        tool: ProcessingTool,
        arguments_dicts: list[dict[str, Any]],
        workflow: Any,
        node_name: str,
        has_batch: bool,
        row_contexts: list[ExecutionContext],
        batch_context: ExecutionContext,
    ) -> list[list[Any]]:
        """Dispatch through Wetlands — tool runs in isolated environment workers."""
        from bioimageflow.worker_origins import resolve_worker_tool_origin
        from wetlands.task import TaskStatus, TaskEventType

        assert self._env_manager is not None
        if (
            has_batch
            and not arguments_dicts
            and not getattr(tool, "run_empty_batch", False)
        ):
            return []
        env_spec = tool.environment
        origin = resolve_worker_tool_origin(tool)
        invocation_id = f"inv_{uuid.uuid4().hex}"
        max_workers, worker_env, worker_timeout = self._resolve_worker_config(
            tool, workflow
        )
        engine_timeout = _compute_engine_timeout(worker_timeout)

        if has_batch:
            invocation = ProcessingTaskV1(
                task_id="task_0000000000000000",
                node_name=node_name,
                invocation_id=invocation_id,
                cache_attempt_id=None,
                task_retry=0,
                mode="process_batch",
                tool=origin,
                rows=tuple(
                    RowInvocationV1(
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
            )
            task = self._env_manager.submit_processing_task(
                env_spec,
                encode_processing_task(invocation),
                max_workers=max_workers,
                worker_env=worker_env,
                worker_timeout=worker_timeout,
            )
            try:
                task.wait_for(timeout=engine_timeout)
            except TimeoutError:
                self._emit_progress(workflow, node_name, "failed")
                task.cancel()
                raise WorkerTimeoutError(
                    f"Batch task for node '{node_name}' exceeded engine-side "
                    f"timeout ({engine_timeout:.0f}s; "
                    f"worker_timeout={worker_timeout}s)"
                )
            if task.status == TaskStatus.FAILED:
                _raise_worker_task_error(
                    task,
                    node_name=node_name,
                    tool=tool,
                    row_index=None,
                )
            if task.status == TaskStatus.CANCELED:
                raise WorkflowCancelledError(
                    "Workflow cancelled during batch execution"
                )
            result = decode_processing_result(task.result)
            validate_processing_result(invocation, result)
            assert tool.Outputs is not None
            return [
                [tool.Outputs(**output) for output in row.outputs]
                for row in result.rows
            ]

        invocations = [
            ProcessingTaskV1(
                task_id=f"task_{position:016x}",
                node_name=node_name,
                invocation_id=invocation_id,
                cache_attempt_id=None,
                task_retry=0,
                mode="row_chunk",
                tool=origin,
                rows=(
                    RowInvocationV1(
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
        tasks = self._env_manager.map_processing_tasks(
            env_spec,
            [encode_processing_task(invocation) for invocation in invocations],
            max_workers=max_workers,
            worker_env=worker_env,
            worker_timeout=worker_timeout,
        )

        # Attach progress listeners for sub-row progress reporting
        for i, task in enumerate(tasks):

            def _make_listener(row_idx):
                def on_event(event):
                    if event.type == TaskEventType.UPDATE:
                        self._emit_progress(
                            workflow,
                            node_name,
                            "row_progress",
                            row=row_idx,
                            total_rows=len(tasks),
                            message=event.task.message,
                            current=event.task.current,
                            maximum=event.task.maximum,
                        )

                return on_event

            task.listen(_make_listener(i))

        # Wait and collect results — fail-fast on first error, cancel, or timeout
        try:
            for i, task in enumerate(tasks):
                if workflow.cancel_requested:
                    raise WorkflowCancelledError("Workflow cancelled by user")
                try:
                    task.wait_for(timeout=engine_timeout)
                except TimeoutError:
                    self._emit_progress(
                        workflow, node_name, "failed", row=i, total_rows=len(tasks)
                    )
                    raise WorkerTimeoutError(
                        f"Task for node '{node_name}' row {i} exceeded "
                        f"engine-side timeout ({engine_timeout:.0f}s; "
                        f"worker_timeout={worker_timeout}s)"
                    )
                if task.status == TaskStatus.FAILED:
                    _raise_worker_task_error(
                        task,
                        node_name=node_name,
                        tool=tool,
                        row_index=row_contexts[i].row_index,
                    )
        except (WorkflowCancelledError, Exception):
            # Cancel all remaining in-flight tasks
            for t in tasks:
                if not t.status.is_finished():
                    t.cancel()
            for t in tasks:
                if not t.status.is_finished():
                    try:
                        t.wait_for(timeout=10)
                    except Exception:
                        pass
            raise

        # Collect results in submission order — skip cancelled tasks
        raw_results: list[list[Any]] = []
        assert tool.Outputs is not None
        for i, task in enumerate(tasks):
            if task.status == TaskStatus.CANCELED:
                continue
            result = decode_processing_result(task.result)
            validate_processing_result(invocations[i], result)
            row_result = result.rows[0]
            raw_results.append(
                [tool.Outputs(**output) for output in row_result.outputs]
            )
            self._emit_progress(
                workflow, node_name, "row_complete", row=i, total_rows=len(tasks)
            )
        return raw_results
