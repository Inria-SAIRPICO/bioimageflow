"""Strict versioned processing-task envelopes and codecs."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import os
from pathlib import Path
import re
from typing import Any, Dict, Literal, Mapping, Optional, Tuple, cast

from bioimageflow_core.arguments import ExecutionContext
from bioimageflow_core.worker_origins import (
    WorkerToolOriginV1,
    decode_worker_tool_origin,
    encode_worker_tool_origin,
)


TASK_SCHEMA = "bioimageflow.processing_task.v1"
RESULT_SCHEMA = "bioimageflow.processing_result.v1"
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
class RowInvocationV1:
    position: int
    row_index: str
    arguments: Dict[str, Any]
    context: Optional[Dict[str, Any]]


@dataclass(frozen=True)
class ProcessingTaskV1:
    task_id: str
    node_name: str
    invocation_id: str
    cache_attempt_id: Optional[str]
    task_retry: int
    mode: Literal["row_chunk", "process_batch"]
    tool: WorkerToolOriginV1
    rows: Tuple[RowInvocationV1, ...]
    batch_context: Optional[Dict[str, Any]] = None
    schema: Literal["bioimageflow.processing_task.v1"] = field(
        default=TASK_SCHEMA, init=False
    )


@dataclass(frozen=True)
class RowResultV1:
    position: int
    row_index: str
    outputs: Tuple[Dict[str, Any], ...]


@dataclass(frozen=True)
class ProcessingTaskResultV1:
    task_id: str
    node_name: str
    invocation_id: str
    cache_attempt_id: Optional[str]
    task_retry: int
    mode: Literal["row_chunk", "process_batch"]
    rows: Tuple[RowResultV1, ...]
    metrics: Optional[Dict[str, Any]] = None
    schema: Literal["bioimageflow.processing_result.v1"] = field(
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


def _require_plain_dict(value: Any, label: str) -> Dict[str, Any]:
    if type(value) is not dict:
        raise ValueError(f"{label} must be a plain object.")
    if not all(isinstance(key, str) for key in value):
        raise ValueError(f"{label} keys must be strings.")
    return dict(value)


def _encode_portable_value(value: Any) -> Any:
    """Convert path values recursively without changing ordinary transport values."""
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {
            key: _encode_portable_value(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_encode_portable_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_encode_portable_value(item) for item in value)
    return value


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


def _decode_row_invocation(payload: Any) -> RowInvocationV1:
    row = _require_plain_dict(payload, "row invocation")
    _require_exact_keys(
        row, {"position", "row_index", "arguments", "context"}, "row invocation"
    )
    return RowInvocationV1(
        position=_require_integer(row["position"], "row position"),
        row_index=_require_row_index(row["row_index"], "row_index"),
        arguments=_require_plain_dict(row["arguments"], "row arguments"),
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


def encode_processing_task(task: ProcessingTaskV1) -> Dict[str, Any]:
    """Encode a processing task to its exact worker-safe object."""
    if not isinstance(task, ProcessingTaskV1):
        raise TypeError("task must be a ProcessingTaskV1 value.")
    payload = asdict(task)
    payload["tool"] = encode_worker_tool_origin(task.tool)
    payload["rows"] = [
        _encode_portable_value(asdict(row))
        for row in task.rows
    ]
    return payload


def decode_processing_task(payload: Mapping[str, Any]) -> ProcessingTaskV1:
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
        "tool",
        "rows",
        "batch_context",
    }
    _require_exact_keys(task, expected, "processing task")
    if task["schema"] != TASK_SCHEMA:
        raise ValueError(f"Unsupported processing task schema: {task['schema']!r}.")
    retry = _require_integer(task["task_retry"], "task_retry")
    if retry != 0:
        raise ValueError("task_retry must be zero.")
    mode = _require_mode(task["mode"])
    rows = _decode_rows(task["rows"], _decode_row_invocation, "task rows")
    batch_context = _decode_context(task["batch_context"], "batch context")
    if mode == "row_chunk" and batch_context is not None:
        raise ValueError("row_chunk tasks must not define batch_context.")
    if mode == "process_batch" and batch_context is None:
        raise ValueError("process_batch tasks require batch_context.")
    return ProcessingTaskV1(
        task_id=_require_identifier(task["task_id"], _TASK_ID_RE, "task_id"),
        node_name=_require_text(task["node_name"], "node_name"),
        invocation_id=_require_identifier(
            task["invocation_id"], _INVOCATION_ID_RE, "invocation_id"
        ),
        cache_attempt_id=_require_optional_attempt_id(task["cache_attempt_id"]),
        task_retry=retry,
        mode=mode,
        tool=decode_worker_tool_origin(task["tool"]),
        rows=rows,
        batch_context=batch_context,
    )


def _decode_row_result(payload: Any) -> RowResultV1:
    row = _require_plain_dict(payload, "row result")
    _require_exact_keys(row, {"position", "row_index", "outputs"}, "row result")
    outputs_value = row["outputs"]
    if type(outputs_value) is not list:
        raise ValueError("row outputs must be an array.")
    outputs = tuple(
        _require_plain_dict(output, "row output") for output in outputs_value
    )
    return RowResultV1(
        position=_require_integer(row["position"], "row position"),
        row_index=_require_row_index(row["row_index"], "row_index"),
        outputs=outputs,
    )


def encode_processing_result(result: ProcessingTaskResultV1) -> Dict[str, Any]:
    """Encode a processing result to its exact orchestrator-safe object."""
    if not isinstance(result, ProcessingTaskResultV1):
        raise TypeError("result must be a ProcessingTaskResultV1 value.")
    payload = asdict(result)
    payload["rows"] = [
        {
            "position": row.position,
            "row_index": row.row_index,
            "outputs": [dict(output) for output in row.outputs],
        }
        for row in result.rows
    ]
    return payload


def decode_processing_result(payload: Mapping[str, Any]) -> ProcessingTaskResultV1:
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
        "rows",
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
    return ProcessingTaskResultV1(
        task_id=_require_identifier(result["task_id"], _TASK_ID_RE, "task_id"),
        node_name=_require_text(result["node_name"], "node_name"),
        invocation_id=_require_identifier(
            result["invocation_id"], _INVOCATION_ID_RE, "invocation_id"
        ),
        cache_attempt_id=_require_optional_attempt_id(result["cache_attempt_id"]),
        task_retry=retry,
        mode=_require_mode(result["mode"]),
        rows=_decode_rows(result["rows"], _decode_row_result, "result rows"),
        metrics=metrics,
    )


def validate_processing_result(
    task: ProcessingTaskV1, result: ProcessingTaskResultV1
) -> None:
    """Require exact task/result correlation and row correspondence."""
    task_fields = (
        task.task_id,
        task.node_name,
        task.invocation_id,
        task.cache_attempt_id,
        task.task_retry,
        task.mode,
    )
    result_fields = (
        result.task_id,
        result.node_name,
        result.invocation_id,
        result.cache_attempt_id,
        result.task_retry,
        result.mode,
    )
    if task_fields != result_fields:
        raise ValueError("Processing result correlation does not match its task.")
    task_rows = tuple((row.position, row.row_index) for row in task.rows)
    result_rows = tuple((row.position, row.row_index) for row in result.rows)
    if task_rows != result_rows:
        raise ValueError("Processing result rows do not match their task.")
