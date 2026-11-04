"""Canonical backend-neutral processing worker entry point."""

from __future__ import annotations

from contextlib import nullcontext
from dataclasses import replace
import inspect
from typing import Any, Dict, Mapping, Optional, Tuple

from bioimageflow_core._processing_values import accept_native_values
from bioimageflow_core.arguments import Arguments, ExecutionContext
from bioimageflow_core.declarations import compare_tool_declarations, declaration_digest, describe_tool_declaration
from bioimageflow_core.import_context import admit_import_root, selected_import_root
from bioimageflow_core.shared_memory import SharedMemoryContext, collect_input_scopes, get_shared_memory_context
from bioimageflow_core.tool import IOModel, ProcessingTool
from bioimageflow_core.worker_origins import VersionedModuleOrigin, _selected_worker_tool
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
    result: Any, output_type: type[IOModel], fields: Tuple[str, ...], task: ProcessingTask
) -> Tuple[Dict[str, Any], ...]:
    outputs = result if isinstance(result, list) else [result]
    return _accept_callback_outputs(tuple(_outputs_to_dict(output, output_type, fields) for output in outputs), task)


def _accept_callback_outputs(outputs: Any, task: ProcessingTask) -> Any:
    if task.shared_memory_context is not None:
        return get_shared_memory_context()._publish_outputs(outputs)
    return accept_native_values(outputs)


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
            outputs=_normalize_row_outputs(output, output_type, fields, task),
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
            outputs=_accept_callback_outputs(tuple(_outputs_to_dict(output, output_type, fields) for output in raw), task),
        ),)
    if raw and not isinstance(raw[0], list):
        if len(raw) != len(task.rows):
            raise ValueError("Flat process_batch output count must match the input row count.")
        grouped = [[output] for output in raw]
    else:
        grouped = raw
        if len(grouped) != len(task.rows):
            raise ValueError("Nested process_batch output groups must match the input row count.")
    accepted = _accept_callback_outputs(tuple(
        tuple(_outputs_to_dict(output, output_type, fields) for output in outputs)
        for outputs in grouped
    ), task)
    return tuple(
        OutputGroup(
            consumed_rows=(ConsumedRow(row.position, row.row_index),),
            outputs=outputs,
        )
        for row, outputs in zip(task.rows, accepted)
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
    if isinstance(origin, VersionedModuleOrigin):
        admission = admit_import_root(origin.store_root, import_package=origin.import_package,
                                      dependency_authority="managed_runtime")
        with selected_import_root(admission):
            return _execute_admitted(invocation, task=task, admission=admission)
    return _execute_admitted(invocation, task=task)


def _execute_admitted(invocation: ProcessingTask, *, task: Any, admission: Any = None) -> Dict[str, Any]:
    with _selected_worker_tool(invocation.tool, admission=admission) as tool:
        return _execute_tool(invocation, tool, task=task)


def _execute_tool(invocation: ProcessingTask, tool: Any, *, task: Any) -> Dict[str, Any]:
    if tool.row_consumption.value != invocation.row_consumption:
        raise ValueError("Task row_consumption does not match the admitted tool declaration.")
    declaration = describe_tool_declaration(tool)
    compare_tool_declarations(invocation.declaration, declaration)
    _validate_output_file_contexts(invocation, declaration)
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


def _validate_output_file_contexts(invocation: ProcessingTask, declaration: Dict[str, Any]) -> None:
    """Admit complete ownership once per batch, or one projection per row chunk."""
    exact = {name: item["output_file"] for name, item in declaration["outputs"]["fields"].items()
             if item["output_file"] is not None and item["output_file"]["role"] == "exact"}
    rows = {row.position: row for row in invocation.rows}
    references = {row.position: row for row in invocation.reference_rows}
    contexts = ([(invocation.batch_context, None)] if invocation.mode == "process_batch" else
                [(row.context, row.position) for row in invocation.rows])
    all_owners: Dict[Any, str] = {}
    all_paths: Dict[str, Any] = {}
    for raw, context_position in contexts:
        if raw is None:
            if exact:
                raise ValueError("Exact writable outputs require an admitted file context")
            continue
        context = ExecutionContext.from_dict(raw)
        if set(context.output_files) != set(exact):
            raise ValueError("Output file context does not match exact output declaration")
        expected_positions = set(rows) if context_position is None else {context_position}
        for name, files in context.output_files.items():
            contract = exact[name]["group"]
            kind, keys = contract["kind"], contract["keys"]
            if invocation.mode == "row_chunk" and kind != "per_row":
                raise ValueError("Shared file groups require whole-node process_batch")
            row_owners: Dict[int, Any] = {}
            reference_owners: Dict[int, Any] = {}
            group_keys = set()
            for item in files:
                if list(item.key) != keys:
                    raise ValueError("Planned file key does not match declared grouping")
                if kind == "per_row" and (len(item.row_positions) != 1 or item.reference_positions
                                          or item.group_index != item.row_positions[0]):
                    raise ValueError("Per-row file must belong to one actual observation")
                if kind == "once" and item.group_index != 0:
                    raise ValueError("Whole-batch file requires one group")
                identity = tuple((key, _file_key_identity(value)) for key, value in item.key.items())
                if kind == "by" and identity in group_keys:
                    raise ValueError("Duplicate keyed file owner")
                group_keys.add(identity)
                for position in item.row_positions:
                    if position not in expected_positions or position in row_owners:
                        raise ValueError("Invalid or duplicated file observation membership")
                    row_owners[position] = item
                for position in item.reference_positions:
                    if kind != "by" or position not in references or position in reference_owners:
                        raise ValueError("Invalid or duplicated file reference membership")
                    reference_owners[position] = item
                owner, path = (name, item.group_index), str(item.path)
                if owner in all_owners and all_owners[owner] != path:
                    raise ValueError("One file owner has inconsistent destinations")
                if path in all_paths and all_paths[path] != owner:
                    raise ValueError("Distinct file owners have the same destination")
                all_owners[owner], all_paths[path] = path, owner
                observations = [rows[position].arguments for position in item.row_positions]
                observations += [references[position].arguments for position in item.reference_positions]
                if not observations:
                    observations = [invocation.batch_arguments]
                if kind == "by":
                    for values in observations:
                        for key, expected in item.key.items():
                            # A same-name exact destination replaces that input
                            # argument; its original source is the admitted key.
                            if key not in exact and not _same_file_key(values.get(key), expected):
                                raise ValueError("File group key disagrees with contributing inputs")
            if set(row_owners) != expected_positions:
                raise ValueError("File plan must cover each observation exactly once")
            for position, item in row_owners.items():
                if not _matches_file_destination(rows[position].arguments.get(name), item.path):
                    raise ValueError("Observation arguments disagree with admitted destination")
            if context_position is None:
                if len({item.group_index for item in files}) != len(files):
                    raise ValueError("Duplicate planned file group index")
                if kind == "by":
                    eligible = set()
                    for position, reference in references.items():
                        present = [key in reference.arguments for key in keys]
                        if any(present) and not all(present):
                            raise ValueError("Reference contains an incomplete composite file key")
                        if all(present):
                            eligible.add(position)
                    if set(reference_owners) != eligible:
                        raise ValueError("Keyed file plan must cover each eligible reference exactly once")
                    for position, item in reference_owners.items():
                        if not _matches_file_destination(references[position].arguments.get(name), item.path):
                            raise ValueError("Reference arguments disagree with admitted destination")
                elif kind == "once":
                    eligible = bool(rows) or invocation.row_consumption == "collective"
                    if len(files) != int(eligible):
                        raise ValueError("Whole-batch file plan has an incorrect owner count")
                    if files and not _matches_file_destination(invocation.batch_arguments.get(name), files[0].path):
                        raise ValueError("Batch arguments disagree with admitted destination")


def _file_key_identity(value: Any) -> Any:
    from pathlib import Path
    if isinstance(value, Path):
        return ("path", str(value))
    if type(value) is float:
        return ("float", value.hex())
    return (type(value).__name__, value)


def _same_file_key(actual: Any, expected: Any) -> bool:
    from pathlib import Path
    if isinstance(expected, Path):
        return isinstance(actual, (str, Path)) and Path(actual) == expected
    if type(actual).__module__.startswith("numpy") and getattr(actual, "ndim", None) == 0:
        actual = actual.item()
    return type(actual) is type(expected) and actual == expected


def _matches_file_destination(value: Any, expected: Any) -> bool:
    from pathlib import Path
    return isinstance(value, (str, Path)) and Path(value) == expected
