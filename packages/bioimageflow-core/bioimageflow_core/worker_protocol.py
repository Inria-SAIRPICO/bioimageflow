"""Strict versioned processing-task envelopes and codecs."""

from __future__ import annotations

from dataclasses import dataclass, field
from copy import deepcopy
import os
from pathlib import Path
import re
from typing import Any, Dict, Literal, Mapping, Optional, Tuple, cast

from bioimageflow_core.arguments import ExecutionContext, ReferenceRow
from bioimageflow_core.shared_memory import validate_scope_descriptor
from bioimageflow_core._processing_values import (
    decode_processing_value,
    encode_processing_value,
)
from bioimageflow_core.worker_origins import (
    WorkerToolOriginV1,
    decode_worker_tool_origin,
    encode_worker_tool_origin,
)


TASK_SCHEMA = "bioimageflow.processing_task.v3"
RESULT_SCHEMA = "bioimageflow.processing_result.v3"
_TASK_ID_RE = re.compile(r"^task_[0-9a-f]{16}$")
_INVOCATION_ID_RE = re.compile(r"^inv_[0-9a-f]{32}$")
_ATTEMPT_ID_RE = re.compile(r"^att_[0-9a-f]{32}$")
_MODES = {"row_chunk", "process_batch"}
_CONTEXT_FIELDS = {
    "run_dir",
    "assets_dir",
    "work_dir",
    "rows_dir",
    "row_dir",
    "batch_dir",
    "row_index",
}


@dataclass(frozen=True)
class RowInvocation:
    position: int
    row_index: str
    arguments: Dict[str, Any]
    context: Optional[Dict[str, Any]]


@dataclass(frozen=True)
class ProcessingTask:
    task_id: str
    node_name: str
    invocation_id: str
    cache_attempt_id: Optional[str]
    task_retry: int
    mode: Literal["row_chunk", "process_batch"]
    row_consumption: Literal["mapped", "collective"]
    tool: WorkerToolOriginV1
    rows: Tuple[RowInvocation, ...]
    batch_context: Optional[Dict[str, Any]] = None
    batch_arguments: Dict[str, Any] = field(default_factory=dict)
    reference_rows: Tuple[ReferenceRow, ...] = ()
    shared_memory_context: Optional[Dict[str, Any]] = None
    schema: Literal["bioimageflow.processing_task.v3"] = field(
        default=TASK_SCHEMA, init=False
    )


@dataclass(frozen=True)
class ConsumedRow:
    position: int
    row_index: str


@dataclass(frozen=True)
class OutputGroup:
    consumed_rows: Tuple[ConsumedRow, ...]
    outputs: Tuple[Dict[str, Any], ...]


@dataclass(frozen=True)
class ProcessingTaskResult:
    task_id: str
    node_name: str
    invocation_id: str
    cache_attempt_id: Optional[str]
    task_retry: int
    mode: Literal["row_chunk", "process_batch"]
    row_consumption: Literal["mapped", "collective"]
    groups: Tuple[OutputGroup, ...]
    metrics: Optional[Dict[str, Any]] = None
    schema: Literal["bioimageflow.processing_result.v3"] = field(
        default=RESULT_SCHEMA, init=False
    )


def _require_exact_keys(payload: Mapping[str, Any], expected: set, label: str) -> None:
    actual = set(payload)
    if actual != expected:
        raise ValueError(
            f"{label} fields do not match the schema; "
            f"missing={sorted(expected - actual)}, extra={sorted(actual - expected)}."
        )


def _require_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{label} must be a non-empty normalized string.")
    if "\x00" in value or any(ord(character) < 32 for character in value):
        raise ValueError(f"{label} contains invalid control characters.")
    return value


def _require_row_index(value: Any, label: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a string.")
    if "\x00" in value or any(ord(character) < 32 for character in value):
        raise ValueError(f"{label} contains invalid control characters.")
    return value


def _require_integer(value: Any, label: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(
            f"{label} must be an integer greater than or equal to {minimum}."
        )
    return value


def _require_identifier(value: Any, pattern: re.Pattern, label: str) -> str:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise ValueError(f"{label} has an invalid format.")
    return value


def _require_optional_attempt_id(value: Any) -> Optional[str]:
    if value is None:
        return None
    return _require_identifier(value, _ATTEMPT_ID_RE, "cache_attempt_id")


def _require_mode(value: Any) -> Literal["row_chunk", "process_batch"]:
    if not isinstance(value, str) or value not in _MODES:
        raise ValueError(f"Unsupported processing mode: {value!r}.")
    return cast(Literal["row_chunk", "process_batch"], value)


def _require_consumption(value: Any) -> Literal["mapped", "collective"]:
    if not isinstance(value, str) or value not in ("mapped", "collective"):
        raise ValueError(f"Unsupported row_consumption: {value!r}.")
    return cast(Literal["mapped", "collective"], value)


def _require_consumption_mode(mode: str, consumption: str) -> None:
    if consumption == "collective" and mode != "process_batch":
        raise ValueError("Collective tasks require process_batch mode.")


def _require_plain_dict(value: Any, label: str) -> Dict[str, Any]:
    if type(value) is not dict:
        raise ValueError(f"{label} must be a plain object.")
    if not all(isinstance(key, str) for key in value):
        raise ValueError(f"{label} keys must be strings.")
    return dict(value)


def _require_path(value: Any, label: str) -> str:
    text = _require_text(value, label)
    if not Path(text).is_absolute() or os.path.normpath(text) != text:
        raise ValueError(f"{label} must be an absolute normalized path.")
    return text


def _decode_context(value: Any, label: str) -> Optional[Dict[str, Any]]:
    if value is None:
        return None
    context = _require_plain_dict(value, label)
    _require_exact_keys(context, _CONTEXT_FIELDS, label)
    for field_name in ("run_dir", "assets_dir", "work_dir", "rows_dir"):
        _require_path(context[field_name], f"{label}.{field_name}")
    for field_name in ("row_dir", "batch_dir"):
        if context[field_name] is not None:
            _require_path(context[field_name], f"{label}.{field_name}")
    if context["row_index"] is not None:
        _require_row_index(context["row_index"], f"{label}.row_index")
    try:
        ExecutionContext.from_dict(context)
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"{label} is not a valid ExecutionContext.") from exc
    return context


def _decode_row_invocation(payload: Any) -> RowInvocation:
    row = _require_plain_dict(payload, "row invocation")
    _require_exact_keys(
        row, {"position", "row_index", "arguments", "context"}, "row invocation"
    )
    return RowInvocation(
        position=_require_integer(row["position"], "row position"),
        row_index=_require_row_index(row["row_index"], "row_index"),
        arguments=_require_plain_dict(
            decode_processing_value(row["arguments"]), "row arguments"
        ),
        context=_decode_context(row["context"], "row context"),
    )


def _decode_rows(value: Any, decoder: Any, label: str) -> Tuple[Any, ...]:
    if type(value) is not list:
        raise ValueError(f"{label} must be an array.")
    rows = tuple(decoder(item) for item in value)
    positions = [row.position for row in rows]
    if len(set(positions)) != len(positions):
        raise ValueError(f"{label} contains duplicate positions.")
    if positions != sorted(positions):
        raise ValueError(f"{label} positions must be in increasing order.")
    return rows


def _decode_shared_memory_context(value: Any) -> Optional[Dict[str, Any]]:
    if value is None:
        return None
    context = _require_plain_dict(value, "shared memory context")
    _require_exact_keys(context, {"output", "inputs"}, "shared memory context")
    output = validate_scope_descriptor(context["output"])
    if type(context["inputs"]) is not list:
        raise ValueError("Shared memory input scopes must be a list.")
    inputs = [validate_scope_descriptor(item) for item in context["inputs"]]
    ids = [item["scope_id"] for item in inputs]
    if len(set(ids)) != len(ids) or output["scope_id"] in ids:
        raise ValueError("Shared memory scope identities must be unique.")
    return {"output": output, "inputs": inputs}


def encode_processing_task(task: ProcessingTask) -> Dict[str, Any]:
    """Encode a processing task to its exact worker-safe object."""
    if not isinstance(task, ProcessingTask):
        raise TypeError("task must be a ProcessingTask value.")
    return {
        "schema": task.schema,
        "task_id": task.task_id,
        "node_name": task.node_name,
        "invocation_id": task.invocation_id,
        "cache_attempt_id": task.cache_attempt_id,
        "task_retry": task.task_retry,
        "mode": task.mode,
        "row_consumption": task.row_consumption,
        "tool": encode_worker_tool_origin(task.tool),
        "rows": [
            {
                "position": row.position,
                "row_index": row.row_index,
                "arguments": encode_processing_value(row.arguments),
                "context": deepcopy(row.context),
            }
            for row in task.rows
        ],
        "batch_context": deepcopy(task.batch_context),
        "batch_arguments": encode_processing_value(task.batch_arguments),
        "reference_rows": [
            {"position": row.position, "row_index": row.row_index,
             "arguments": encode_processing_value(row.arguments)}
            for row in task.reference_rows
        ],
        "shared_memory_context": _decode_shared_memory_context(task.shared_memory_context),
    }


def decode_processing_task(payload: Mapping[str, Any]) -> ProcessingTask:
    """Decode one task and fail closed before any tool code runs."""
    task = _require_plain_dict(payload, "processing task")
    expected = {
        "schema",
        "task_id",
        "node_name",
        "invocation_id",
        "cache_attempt_id",
        "task_retry",
        "mode",
        "row_consumption",
        "tool",
        "rows",
        "batch_context",
        "batch_arguments",
        "reference_rows",
        "shared_memory_context",
    }
    _require_exact_keys(task, expected, "processing task")
    if task["schema"] != TASK_SCHEMA:
        raise ValueError(f"Unsupported processing task schema: {task['schema']!r}.")
    retry = _require_integer(task["task_retry"], "task_retry")
    if retry != 0:
        raise ValueError("task_retry must be zero.")
    mode = _require_mode(task["mode"])
    consumption = _require_consumption(task["row_consumption"])
    _require_consumption_mode(mode, consumption)
    rows = _decode_rows(task["rows"], _decode_row_invocation, "task rows")
    batch_context = _decode_context(task["batch_context"], "batch context")
    if mode == "row_chunk" and batch_context is not None:
        raise ValueError("row_chunk tasks must not define batch_context.")
    if mode == "process_batch" and batch_context is None:
        raise ValueError("process_batch tasks require batch_context.")
    batch_arguments = _require_plain_dict(
        decode_processing_value(task["batch_arguments"]), "batch arguments"
    )
    reference_rows = _decode_rows(task["reference_rows"], _decode_reference_row, "reference rows")
    if mode == "row_chunk" and (batch_arguments or reference_rows):
        raise ValueError("row_chunk tasks must not define batch arguments or references.")
    return ProcessingTask(
        task_id=_require_identifier(task["task_id"], _TASK_ID_RE, "task_id"),
        node_name=_require_text(task["node_name"], "node_name"),
        invocation_id=_require_identifier(
            task["invocation_id"], _INVOCATION_ID_RE, "invocation_id"
        ),
        cache_attempt_id=_require_optional_attempt_id(task["cache_attempt_id"]),
        task_retry=retry,
        mode=mode,
        row_consumption=consumption,
        tool=decode_worker_tool_origin(task["tool"]),
        rows=rows,
        batch_context=batch_context,
        batch_arguments=batch_arguments,
        reference_rows=reference_rows,
        shared_memory_context=_decode_shared_memory_context(task["shared_memory_context"]),
    )


def _decode_reference_row(payload: Any) -> ReferenceRow:
    row = _require_plain_dict(payload, "reference row")
    _require_exact_keys(row, {"position", "row_index", "arguments"}, "reference row")
    return ReferenceRow(
        position=_require_integer(row["position"], "reference position"),
        row_index=_require_row_index(row["row_index"], "reference row_index"),
        arguments=_require_plain_dict(decode_processing_value(row["arguments"]), "reference arguments"),
    )


def _decode_consumed_row(payload: Any) -> ConsumedRow:
    row = _require_plain_dict(payload, "consumed row")
    _require_exact_keys(row, {"position", "row_index"}, "consumed row")
    return ConsumedRow(
        position=_require_integer(row["position"], "consumed position"),
        row_index=_require_row_index(row["row_index"], "consumed row_index"),
    )


def _decode_output_group(payload: Any) -> OutputGroup:
    group = _require_plain_dict(payload, "output group")
    _require_exact_keys(group, {"consumed_rows", "outputs"}, "output group")
    if type(group["outputs"]) is not list:
        raise ValueError("Group outputs must be an array.")
    return OutputGroup(
        consumed_rows=_decode_rows(group["consumed_rows"], _decode_consumed_row, "consumed rows"),
        outputs=tuple(_require_plain_dict(decode_processing_value(output), "group output")
                      for output in group["outputs"]),
    )


def encode_processing_result(result: ProcessingTaskResult) -> Dict[str, Any]:
    """Encode a processing result to its exact orchestrator-safe object."""
    if not isinstance(result, ProcessingTaskResult):
        raise TypeError("result must be a ProcessingTaskResult value.")
    return {
        "schema": result.schema,
        "task_id": result.task_id,
        "node_name": result.node_name,
        "invocation_id": result.invocation_id,
        "cache_attempt_id": result.cache_attempt_id,
        "task_retry": result.task_retry,
        "mode": result.mode,
        "row_consumption": result.row_consumption,
        "groups": [
            {"consumed_rows": [
                {"position": row.position, "row_index": row.row_index}
                for row in group.consumed_rows
             ], "outputs": [encode_processing_value(output) for output in group.outputs]}
            for group in result.groups
        ],
        "metrics": deepcopy(result.metrics),
    }


def decode_processing_result(payload: Mapping[str, Any]) -> ProcessingTaskResult:
    """Decode one result and fail closed before output acceptance."""
    result = _require_plain_dict(payload, "processing result")
    expected = {
        "schema",
        "task_id",
        "node_name",
        "invocation_id",
        "cache_attempt_id",
        "task_retry",
        "mode",
        "row_consumption",
        "groups",
        "metrics",
    }
    _require_exact_keys(result, expected, "processing result")
    if result["schema"] != RESULT_SCHEMA:
        raise ValueError(f"Unsupported processing result schema: {result['schema']!r}.")
    retry = _require_integer(result["task_retry"], "task_retry")
    if retry != 0:
        raise ValueError("task_retry must be zero.")
    metrics = result["metrics"]
    if metrics is not None:
        metrics = _require_plain_dict(metrics, "result metrics")
    mode = _require_mode(result["mode"])
    consumption = _require_consumption(result["row_consumption"])
    _require_consumption_mode(mode, consumption)
    if type(result["groups"]) is not list:
        raise ValueError("Result groups must be an array.")
    groups = tuple(_decode_output_group(group) for group in result["groups"])
    if consumption == "collective":
        if len(groups) != 1:
            raise ValueError("Collective results require exactly one output group.")
    else:
        if any(len(group.consumed_rows) != 1 for group in groups):
            raise ValueError("Mapped output groups require one consumed row each.")
        positions = [group.consumed_rows[0].position for group in groups]
        if positions != sorted(set(positions)):
            raise ValueError("Mapped group rows must have unique increasing positions.")
    return ProcessingTaskResult(
        task_id=_require_identifier(result["task_id"], _TASK_ID_RE, "task_id"),
        node_name=_require_text(result["node_name"], "node_name"),
        invocation_id=_require_identifier(
            result["invocation_id"], _INVOCATION_ID_RE, "invocation_id"
        ),
        cache_attempt_id=_require_optional_attempt_id(result["cache_attempt_id"]),
        task_retry=retry,
        mode=mode,
        row_consumption=consumption,
        groups=groups,
        metrics=metrics,
    )


def validate_processing_result(
    task: ProcessingTask, result: ProcessingTaskResult
) -> None:
    """Require exact task/result correlation and row correspondence."""
    task_fields = (
        task.task_id,
        task.node_name,
        task.invocation_id,
        task.cache_attempt_id,
        task.task_retry,
        task.mode,
        task.row_consumption,
    )
    result_fields = (
        result.task_id,
        result.node_name,
        result.invocation_id,
        result.cache_attempt_id,
        result.task_retry,
        result.mode,
        result.row_consumption,
    )
    if task_fields != result_fields:
        raise ValueError("Processing result correlation does not match its task.")
    task_rows = tuple((row.position, row.row_index) for row in task.rows)
    result_rows = tuple(
        tuple((row.position, row.row_index) for row in group.consumed_rows)
        for group in result.groups
    )
    expected = (task_rows,) if task.row_consumption == "collective" else tuple((row,) for row in task_rows)
    if expected != result_rows:
        raise ValueError("Processing result rows do not match their task.")
