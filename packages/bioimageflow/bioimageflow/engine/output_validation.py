"""Canonical ProcessingTool output validation in the orchestrator."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, get_type_hints

from pydantic import ConfigDict, PydanticUserError, TypeAdapter, ValidationError

from bioimageflow.validation import is_path_type
from bioimageflow_core import IOModel
from bioimageflow_core.types import SharedArray
from bioimageflow_core.worker_protocol import RowResultV1


def _resolved_annotations(output_type: type[IOModel]) -> dict[str, Any]:
    declared = output_type._get_all_annotations()
    try:
        resolved = get_type_hints(output_type, include_extras=True)
    except (NameError, TypeError):
        resolved = declared
    return {name: resolved.get(name, annotation) for name, annotation in declared.items()}


def _output_values(
    output: IOModel | Mapping[str, Any],
    output_type: type[IOModel],
) -> dict[str, Any]:
    fields = tuple(output_type._get_all_annotations())
    if isinstance(output, output_type):
        return {field: getattr(output, field) for field in fields}
    if type(output) is not dict:
        raise TypeError(
            f"Processing output must be {output_type.__name__} or a plain dictionary, "
            f"not {type(output).__name__}."
        )
    actual = set(output)
    expected = set(fields)
    if actual != expected:
        raise ValueError(
            "Processing output fields do not match the declared Outputs; "
            f"missing={sorted(expected - actual)}, extra={sorted(actual - expected)}."
        )
    return {field: output[field] for field in fields}


def validate_processing_output(
    output: IOModel | Mapping[str, Any],
    output_type: type[IOModel],
    *,
    reject_shared_array: bool = False,
) -> IOModel:
    """Validate one output and restore its declared field order and runtime values."""
    values = _output_values(output, output_type)
    annotations = _resolved_annotations(output_type)
    validated: dict[str, Any] = {}
    for field, annotation in annotations.items():
        value = values[field]
        if reject_shared_array and isinstance(value, SharedArray):
            raise TypeError(
                f"Processing output field {field!r} returned SharedArray across "
                "an unsupported remote boundary."
            )
        if is_path_type(annotation) and isinstance(value, str):
            value = Path(value)
        try:
            try:
                adapter = TypeAdapter(
                    annotation,
                    config=ConfigDict(
                        strict=True,
                        arbitrary_types_allowed=True,
                    ),
                )
            except PydanticUserError:
                adapter = TypeAdapter(annotation)
            validated[field] = adapter.validate_python(value, strict=True)
        except (PydanticUserError, ValidationError, TypeError, ValueError) as exc:
            raise TypeError(
                f"Processing output field {field!r} does not match its declared "
                f"annotation: {exc}"
            ) from exc
    return output_type(**validated)


def normalize_processing_row_outputs(
    result: Any,
    output_type: type[IOModel],
    *,
    reject_shared_array: bool = False,
) -> list[IOModel]:
    """Normalize one process_row return to the canonical output list."""
    outputs = result if isinstance(result, list) else [result]
    return [
        validate_processing_output(
            output,
            output_type,
            reject_shared_array=reject_shared_array,
        )
        for output in outputs
    ]


def normalize_processing_batch_outputs(
    result: Any,
    output_type: type[IOModel],
    *,
    expected_rows: int,
    reject_shared_array: bool = False,
) -> list[list[IOModel]]:
    """Normalize a process_batch return and enforce exact input cardinality."""
    if not isinstance(result, list):
        raise TypeError("process_batch must return a list.")
    if result and isinstance(result[0], list):
        if not all(isinstance(group, list) for group in result):
            raise TypeError(
                "process_batch must not mix flat and nested outputs."
            )
        grouped = result
        label = "Nested process_batch output groups"
    else:
        if any(isinstance(output, list) for output in result):
            raise TypeError("process_batch must not mix flat and nested outputs.")
        grouped = [[output] for output in result]
        label = "Flat process_batch outputs"
    if len(grouped) != expected_rows:
        raise ValueError(
            f"{label} must match the input row count; "
            f"expected {expected_rows}, received {len(grouped)}."
        )
    return [
        [
            validate_processing_output(
                output,
                output_type,
                reject_shared_array=reject_shared_array,
            )
            for output in group
        ]
        for group in grouped
    ]


def validate_processing_result_rows(
    rows: Sequence[RowResultV1],
    output_type: type[IOModel],
    *,
    reject_shared_array: bool = False,
) -> list[list[IOModel]]:
    """Validate ordered plain worker results after envelope correlation."""
    return [
        [
            validate_processing_output(
                output,
                output_type,
                reject_shared_array=reject_shared_array,
            )
            for output in row.outputs
        ]
        for row in rows
    ]


__all__ = [
    "normalize_processing_batch_outputs",
    "normalize_processing_row_outputs",
    "validate_processing_output",
    "validate_processing_result_rows",
]
