"""Canonical backend-neutral processing worker entry point."""

from __future__ import annotations

import inspect
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

from bioimageflow_core.arguments import Arguments, ExecutionContext
from bioimageflow_core.tool import IOModel, ProcessingTool
from bioimageflow_core.worker_origins import load_worker_tool
from bioimageflow_core.worker_protocol import (
    ProcessingTaskResultV1,
    ProcessingTaskV1,
    RowResultV1,
    decode_processing_task,
    encode_processing_result,
)


def _accepts_keyword(method: Any, keyword: str) -> bool:
    return keyword in inspect.signature(method).parameters


def _outputs_to_dict(output: IOModel, output_type: type) -> Dict[str, Any]:
    if not isinstance(output, output_type):
        raise TypeError(
            f"Tool returned {type(output).__name__}; expected {output_type.__name__}."
        )
    values: Dict[str, Any] = {}
    for name in output._get_all_annotations():
        value = getattr(output, name)
        values[name] = str(value) if isinstance(value, Path) else value
    return values


def _normalize_row_outputs(
    result: Any, output_type: type
) -> Tuple[Dict[str, Any], ...]:
    outputs = result if isinstance(result, list) else [result]
    return tuple(_outputs_to_dict(output, output_type) for output in outputs)


def _row_kwargs(
    tool: ProcessingTool,
    context: Optional[Dict[str, Any]],
    remote_task: Any,
) -> Dict[str, Any]:
    kwargs: Dict[str, Any] = {}
    if context is not None and _accepts_keyword(tool.process_row, "context"):
        kwargs["context"] = ExecutionContext.from_dict(context)
    if remote_task is not None and _accepts_keyword(tool.process_row, "task"):
        kwargs["task"] = remote_task
    return kwargs


def _batch_kwargs(
    tool: ProcessingTool,
    context: Optional[Dict[str, Any]],
    remote_task: Any,
) -> Dict[str, Any]:
    kwargs: Dict[str, Any] = {}
    if context is not None and _accepts_keyword(tool.process_batch, "context"):
        kwargs["context"] = ExecutionContext.from_dict(context)
    if remote_task is not None and _accepts_keyword(tool.process_batch, "task"):
        kwargs["task"] = remote_task
    return kwargs


def _execute_rows(
    task: ProcessingTaskV1,
    tool: ProcessingTool,
    output_type: type,
    remote_task: Any,
) -> Tuple[RowResultV1, ...]:
    results: List[RowResultV1] = []
    for row in sorted(task.rows, key=lambda item: item.position):
        output = tool.process_row(
            Arguments(**row.arguments),
            **_row_kwargs(tool, row.context, remote_task),
        )
        results.append(
            RowResultV1(
                position=row.position,
                row_index=row.row_index,
                outputs=_normalize_row_outputs(output, output_type),
            )
        )
    return tuple(results)


def _execute_batch(
    task: ProcessingTaskV1,
    tool: ProcessingTool,
    output_type: type,
    remote_task: Any,
) -> Tuple[RowResultV1, ...]:
    ordered_rows = tuple(sorted(task.rows, key=lambda item: item.position))
    raw = tool.process_batch(
        [Arguments(**row.arguments) for row in ordered_rows],
        **_batch_kwargs(tool, task.batch_context, remote_task),
    )
    if not isinstance(raw, list):
        raise TypeError("process_batch must return a list.")
    if raw and not isinstance(raw[0], list):
        if len(raw) != len(ordered_rows):
            raise ValueError(
                "Flat process_batch output count must match the input row count."
            )
        grouped = [[output] for output in raw]
    else:
        grouped = raw
        if len(grouped) != len(ordered_rows):
            raise ValueError(
                "Nested process_batch output groups must match the input row count."
            )
    return tuple(
        RowResultV1(
            position=row.position,
            row_index=row.row_index,
            outputs=tuple(
                _outputs_to_dict(output, output_type) for output in row_outputs
            ),
        )
        for row, row_outputs in zip(ordered_rows, grouped)
    )


def execute_processing_task(
    payload: Mapping[str, Any], *, task: Any = None
) -> Dict[str, Any]:
    """Decode, execute, and encode one strict processing-task envelope."""
    invocation = decode_processing_task(payload)
    tool = load_worker_tool(invocation.tool)
    output_type = tool.Outputs
    if output_type is None:
        raise TypeError(f"{type(tool).__name__} does not declare Outputs.")
    if invocation.mode == "row_chunk":
        rows = _execute_rows(invocation, tool, output_type, task)
    else:
        rows = _execute_batch(invocation, tool, output_type, task)
    return encode_processing_result(
        ProcessingTaskResultV1(
            task_id=invocation.task_id,
            node_name=invocation.node_name,
            invocation_id=invocation.invocation_id,
            cache_attempt_id=invocation.cache_attempt_id,
            task_retry=invocation.task_retry,
            mode=invocation.mode,
            rows=rows,
        )
    )
