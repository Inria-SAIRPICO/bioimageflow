"""Canonical backend-neutral processing worker entry point."""

from __future__ import annotations

from contextlib import nullcontext
from dataclasses import replace
import inspect
from typing import Any, Dict, Mapping, Optional, Tuple

from bioimageflow_core.arguments import Arguments, ExecutionContext
from bioimageflow_core.declarations import compare_tool_declarations, declaration_digest, describe_tool_declaration
from bioimageflow_core.import_context import admit_import_root, selected_import_root
from bioimageflow_core.shared_memory import SharedMemoryContext, collect_input_scopes
from bioimageflow_core.tool import IOModel, ProcessingTool
from bioimageflow_core.worker_origins import VersionedModuleOriginV1, _load_worker_tool, load_worker_tool
from bioimageflow_core.worker_protocol import (
    ConsumedRow,
    OutputGroup,
    ProcessingTaskResult,
    ProcessingTask,
    decode_processing_task,
    encode_processing_result,
)


def _outputs_to_dict(
    output: IOModel, output_type: type[IOModel], fields: Tuple[str, ...]
) -> Dict[str, Any]:
    if not isinstance(output, output_type):
        raise TypeError(
            f"Tool returned {type(output).__name__}; expected {output_type.__name__}."
        )
    names = fields if type(output) is output_type else tuple(output._get_all_annotations())
    return {name: getattr(output, name) for name in names}


def _normalize_row_outputs(
    result: Any, output_type: type[IOModel], fields: Tuple[str, ...]
) -> Tuple[Dict[str, Any], ...]:
    outputs = result if isinstance(result, list) else [result]
    return tuple(_outputs_to_dict(output, output_type, fields) for output in outputs)


def _call_kwargs(
    parameters: Mapping[str, Any],
    context: Optional[Dict[str, Any]],
    remote_task: Any,
    invocation: Optional[ProcessingTask] = None,
) -> Dict[str, Any]:
    kwargs: Dict[str, Any] = {}
    if context is not None and "context" in parameters:
        execution_context = ExecutionContext.from_dict(context)
        if invocation is not None:
            execution_context = replace(
                execution_context,
                batch_arguments=Arguments(**invocation.batch_arguments),
                reference_rows=invocation.reference_rows,
            )
        kwargs["context"] = execution_context
    if remote_task is not None and "task" in parameters:
        kwargs["task"] = remote_task
    return kwargs


def _consumed_rows(task: ProcessingTask) -> Tuple[ConsumedRow, ...]:
    return tuple(ConsumedRow(row.position, row.row_index) for row in task.rows)


def _execute_rows(
    task: ProcessingTask,
    tool: ProcessingTool,
    output_type: type[IOModel],
    fields: Tuple[str, ...],
    remote_task: Any,
) -> Tuple[OutputGroup, ...]:
    method = tool.process_row
    parameters = inspect.signature(method).parameters
    results = []
    for row in task.rows:
        output = method(
            Arguments(**row.arguments),
            **_call_kwargs(parameters, row.context, remote_task),
        )
        results.append(OutputGroup(
            consumed_rows=(ConsumedRow(row.position, row.row_index),),
            outputs=_normalize_row_outputs(output, output_type, fields),
        ))
    return tuple(results)


def _execute_batch(
    task: ProcessingTask,
    tool: ProcessingTool,
    output_type: type[IOModel],
    fields: Tuple[str, ...],
    remote_task: Any,
) -> Tuple[OutputGroup, ...]:
    method = tool.process_batch
    raw = method(
        [Arguments(**row.arguments) for row in task.rows],
        **_call_kwargs(inspect.signature(method).parameters, task.batch_context, remote_task, task),
    )
    if not isinstance(raw, list):
        raise TypeError("process_batch must return a list.")
    if task.row_consumption == "collective":
        return (OutputGroup(
            consumed_rows=_consumed_rows(task),
            outputs=tuple(_outputs_to_dict(output, output_type, fields) for output in raw),
        ),)
    if raw and not isinstance(raw[0], list):
        if len(raw) != len(task.rows):
            raise ValueError("Flat process_batch output count must match the input row count.")
        grouped = [[output] for output in raw]
    else:
        grouped = raw
        if len(grouped) != len(task.rows):
            raise ValueError("Nested process_batch output groups must match the input row count.")
    return tuple(
        OutputGroup(
            consumed_rows=(ConsumedRow(row.position, row.row_index),),
            outputs=tuple(_outputs_to_dict(output, output_type, fields) for output in outputs),
        )
        for row, outputs in zip(task.rows, grouped)
    )


def execute_processing_task(
    payload: Mapping[str, Any], *, task: Any = None
) -> Dict[str, Any]:
    """Decode, execute, and encode one strict processing-task envelope."""
    invocation = decode_processing_task(payload)
    scope = invocation.shared_memory_context
    if scope is None:
        # Pure decoded references have no local owner: refuse them before
        # importing trusted tool code, while ordinary values remain valid.
        for row in invocation.rows:
            collect_input_scopes(row.arguments)
        collect_input_scopes(invocation.batch_arguments)
        for reference in invocation.reference_rows:
            collect_input_scopes(reference.arguments)
    runtime = (
        SharedMemoryContext.borrow(scope["output"], inputs=scope["inputs"])
        if scope is not None else None
    )
    with runtime.activate() if runtime is not None else nullcontext():
        if runtime is not None:
            invocation = replace(
                invocation,
                rows=tuple(replace(row, arguments=runtime.bind_value(row.arguments))
                           for row in invocation.rows),
                batch_arguments=runtime.bind_value(invocation.batch_arguments),
                reference_rows=tuple(replace(row, arguments=runtime.bind_value(row.arguments))
                                     for row in invocation.reference_rows),
            )
        return _execute_bound(invocation, task=task)


def _execute_bound(invocation: ProcessingTask, *, task: Any) -> Dict[str, Any]:
    origin = invocation.tool
    if isinstance(origin, VersionedModuleOriginV1):
        admission = admit_import_root(origin.store_root, import_package=origin.import_package,
                                      dependency_authority="managed_runtime")
        with selected_import_root(admission):
            return _execute_admitted(invocation, task=task, admission=admission)
    return _execute_admitted(invocation, task=task)


def _execute_admitted(invocation: ProcessingTask, *, task: Any, admission: Any = None) -> Dict[str, Any]:
    tool = (_load_worker_tool(invocation.tool, admission=admission) if admission is not None
            else load_worker_tool(invocation.tool, dependency_authority="managed_runtime"))
    if tool.row_consumption.value != invocation.row_consumption:
        raise ValueError("Task row_consumption does not match the admitted tool declaration.")
    declaration = describe_tool_declaration(tool)
    compare_tool_declarations(invocation.declaration, declaration)
    output_type = tool.Outputs
    if output_type is None:
        raise TypeError(f"{type(tool).__name__} does not declare Outputs.")
    fields = tuple(declaration["outputs"]["field_names"])
    if invocation.mode == "row_chunk":
        groups = _execute_rows(invocation, tool, output_type, fields, task)
    else:
        groups = _execute_batch(invocation, tool, output_type, fields, task)
    return encode_processing_result(ProcessingTaskResult(
        task_id=invocation.task_id,
        node_name=invocation.node_name,
        invocation_id=invocation.invocation_id,
        cache_attempt_id=invocation.cache_attempt_id,
        task_retry=invocation.task_retry,
        mode=invocation.mode,
        row_consumption=invocation.row_consumption,
        declaration_digest=declaration_digest(declaration),
        groups=groups,
    ))
