"""Canonical processing entry-point tests."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from bioimageflow_core import (
    ProcessingTask,
    IOModel,
    describe_io_model,
    RowInvocation,
    SourceFileOriginV1,
    decode_processing_result,
    encode_processing_task,
    validate_processing_result,
)
from bioimageflow_core.worker import execute_processing_task
from bioimageflow_core.worker_origins import clear_worker_tool_instances


@pytest.fixture(autouse=True)
def _clear_instances():
    clear_worker_tool_instances()
    yield
    clear_worker_tool_instances()


def _context(run_dir: Path, *, row_index: str | None) -> dict[str, str | None]:
    return {
        "run_dir": str(run_dir),
        "assets_dir": str(run_dir / "assets"),
        "work_dir": str(run_dir / "work"),
        "rows_dir": str(run_dir / "work" / "rows"),
        "row_dir": (
            str(run_dir / "work" / "rows" / "000000") if row_index is not None else None
        ),
        "batch_dir": (
            None if row_index is not None else str(run_dir / "work" / "batch")
        ),
        "row_index": row_index,
    }


def _declaration(inputs=None, outputs=None):
    def model(fields):
        return type("Declaration", (IOModel,), {"__annotations__": fields or {}})
    return {"inputs": describe_io_model(model(inputs)), "outputs": describe_io_model(model(outputs))}


def _origin(source: Path) -> SourceFileOriginV1:
    return SourceFileOriginV1(
        path=str(source.resolve()),
        source_hash=hashlib.sha256(source.read_bytes()).hexdigest(),
        class_name="ContextTool",
    )


def test_row_chunk_forwards_context_and_returns_plain_outputs(tmp_path) -> None:
    source = tmp_path / "context_tool.py"
    source.write_text(
        """
from bioimageflow_core import Arguments, ExecutionContext, IOModel, ProcessingTool, RowConsumption

class ContextTool(ProcessingTool):
    row_consumption = RowConsumption.MAPPED
    class Inputs(IOModel):
        value: str
    class Outputs(IOModel):
        seen: str
    def process_row(self, arguments: Arguments, *, context: ExecutionContext):
        assert context.row_index == "sample"
        return self.Outputs(seen=str(context.row_dir / arguments.value))
""",
        encoding="utf-8",
    )
    run_dir = (tmp_path / "run").resolve()
    invocation = ProcessingTask(
        task_id="task_0000000000000000",
        node_name="context",
        invocation_id=f"inv_{'1' * 32}",
        cache_attempt_id=None,
        task_retry=0,
        mode="row_chunk", row_consumption="mapped",
        tool=_origin(source),
        declaration=_declaration({"value": str}, {"seen": str}),
        rows=(
            RowInvocation(
                position=0,
                row_index="sample",
                arguments={"value": "marker"},
                context=_context(run_dir, row_index="sample"),
            ),
        ),
    )
    result = decode_processing_result(
        execute_processing_task(encode_processing_task(invocation))
    )
    validate_processing_result(invocation, result)
    assert result.groups[0].outputs == (
        {"seen": str(run_dir / "work" / "rows" / "000000" / "marker")},
    )


def test_batch_one_to_one_shorthand_is_normalized(tmp_path) -> None:
    source = tmp_path / "context_tool.py"
    source.write_text(
        """
from bioimageflow_core import Arguments, ExecutionContext, IOModel, ProcessingTool, RowConsumption

class ContextTool(ProcessingTool):
    row_consumption = RowConsumption.MAPPED
    class Inputs(IOModel):
        value: str
    class Outputs(IOModel):
        seen: str
    def process_batch(self, arguments_list, *, context: ExecutionContext):
        return [
            self.Outputs(seen=str(context.batch_dir / arguments.value))
            for arguments in arguments_list
        ]
""",
        encoding="utf-8",
    )
    run_dir = (tmp_path / "run").resolve()
    invocation = ProcessingTask(
        task_id="task_0000000000000000",
        node_name="context",
        invocation_id=f"inv_{'1' * 32}",
        cache_attempt_id=f"att_{'2' * 32}",
        task_retry=0,
        mode="process_batch", row_consumption="mapped",
        tool=_origin(source),
        declaration=_declaration({"value": str}, {"seen": str}),
        rows=tuple(
            RowInvocation(
                position=position,
                row_index=index,
                arguments={"value": index},
                context=_context(run_dir, row_index=index),
            )
            for position, index in enumerate(("a", "b"))
        ),
        batch_context=_context(run_dir, row_index=None),
    )
    result = decode_processing_result(
        execute_processing_task(encode_processing_task(invocation))
    )
    validate_processing_result(invocation, result)
    assert [row.outputs for row in result.groups] == [
        ({"seen": str(run_dir / "work" / "batch" / "a")},),
        ({"seen": str(run_dir / "work" / "batch" / "b")},),
    ]


def test_malformed_payload_fails_before_tool_module_executes(tmp_path) -> None:
    marker = tmp_path / "executed"
    source = tmp_path / "context_tool.py"
    source.write_text(
        f"""
from pathlib import Path
Path({str(marker)!r}).write_text("executed")
""",
        encoding="utf-8",
    )
    invocation = ProcessingTask(
        task_id="task_0000000000000000",
        node_name="context",
        invocation_id=f"inv_{'1' * 32}",
        cache_attempt_id=None,
        task_retry=0,
        mode="row_chunk", row_consumption="mapped",
        tool=_origin(source),
        declaration=_declaration(),
        rows=(),
    )
    payload = encode_processing_task(invocation)
    payload["future"] = True
    with pytest.raises(ValueError):
        execute_processing_task(payload)
    assert not marker.exists()


@pytest.mark.parametrize("location", ["row", "batch", "reference"])
@pytest.mark.parametrize("scope_mode", ["unadmitted", "missing"])
def test_unadmitted_scope_is_refused_before_trusted_tool_import(tmp_path, location, scope_mode):
    from bioimageflow_core import ReferenceRow, SharedArray, SharedMemoryContext
    owner = SharedMemoryContext(tmp_path / "owned")
    sentinel = tmp_path / "executed"
    source = tmp_path / "tool.py"
    source.write_text(f"from pathlib import Path\nPath({str(sentinel)!r}).write_text('executed')\n")
    invocation = ProcessingTask(
        task_id="task_0000000000000000", node_name="scope", invocation_id="inv_" + "1" * 32,
        cache_attempt_id=None, task_retry=0, mode="row_chunk", row_consumption="mapped", tool=_origin(source), declaration=_declaration(),
        rows=(RowInvocation(position=0, row_index="sample", arguments={
            "reference": SharedArray("valid", (1,), "u1", "unadmitted"),
        }, context=None),),
        shared_memory_context={"output": owner.descriptor(), "inputs": []},
    )
    if location != "row":
        from dataclasses import replace
        arguments = invocation.rows[0].arguments
        invocation = replace(invocation, mode="process_batch", rows=(),
                             batch_context=_context((tmp_path / "run").resolve(), row_index=None),
                             batch_arguments=arguments if location == "batch" else {},
                             reference_rows=(ReferenceRow(0, "aux", arguments),) if location == "reference" else ())
    if scope_mode == "missing":
        from dataclasses import replace
        invocation = replace(invocation, shared_memory_context=None)
    try:
        with pytest.raises(ValueError, match="scope"):
            execute_processing_task(encode_processing_task(invocation))
        assert not sentinel.exists()
        assert list((tmp_path / "owned").rglob("*.npy")) == []
    finally:
        owner.close()


@pytest.mark.parametrize("values", [(1, 2, 3), ()], ids=["three-to-one", "empty-to-one"])
def test_collective_worker_emits_one_all_consumed_group(tmp_path, values):
    source = tmp_path / "collective_tool.py"
    marker = tmp_path / "calls"
    source.write_text(f"""
from pathlib import Path
from bioimageflow_core import IOModel, ProcessingTool, RowConsumption
class ContextTool(ProcessingTool):
    row_consumption = RowConsumption.COLLECTIVE
    class Inputs(IOModel):
        value: int
    class Outputs(IOModel):
        total: int
    def process_batch(self, arguments_list, *, context=None):
        marker = Path({str(marker)!r})
        marker.write_text(marker.read_text() + "call\\n" if marker.exists() else "call\\n")
        return [self.Outputs(total=sum(item.value for item in arguments_list))]
""")
    invocation = ProcessingTask(
        task_id="task_0000000000000000", node_name="aggregate",
        invocation_id="inv_" + "1" * 32, cache_attempt_id=None,
        task_retry=0, mode="process_batch", row_consumption="collective", tool=_origin(source), declaration=_declaration({"value": int}, {"total": int}),
        rows=tuple(RowInvocation(position=i, row_index=f"sample-{i}",
                                 arguments={"value": value}, context=None)
                   for i, value in enumerate(values)),
        batch_context=_context((tmp_path / "run").resolve(), row_index=None),
    )
    result = decode_processing_result(execute_processing_task(encode_processing_task(invocation)))
    validate_processing_result(invocation, result)
    assert marker.read_text().splitlines() == ["call"]
    assert len(result.groups) == 1
    assert result.groups[0].outputs == ({"total": sum(values)},)
    assert tuple((row.position, row.row_index) for row in result.groups[0].consumed_rows) == tuple(
        (i, f"sample-{i}") for i in range(len(values))
    )


@pytest.mark.parametrize("consumption", ["mapped", "collective"])
def test_batch_expansion_preserves_consumed_association(tmp_path, consumption):
    source = tmp_path / "tool.py"
    source.write_text(f"""
from bioimageflow_core import IOModel, ProcessingTool, RowConsumption
class ContextTool(ProcessingTool):
    row_consumption = RowConsumption.{consumption.upper()}
    class Outputs(IOModel):
        value: int
    def process_batch(self, arguments_list, *, context=None):
        if self.row_consumption is RowConsumption.COLLECTIVE:
            return []
        return [[], [self.Outputs(value=2)], [self.Outputs(value=3), self.Outputs(value=4)]]
""")
    invocation = ProcessingTask(
        task_id="task_" + "0" * 16, node_name="expansion", invocation_id="inv_" + "1" * 32,
        cache_attempt_id=None, task_retry=0, mode="process_batch", row_consumption=consumption,
        tool=_origin(source), declaration=_declaration(outputs={"value": int}), rows=tuple(RowInvocation(i, f"sample-{i}", {}, None) for i in range(3)),
        batch_context=_context((tmp_path / "run").resolve(), row_index=None),
    )
    result = decode_processing_result(execute_processing_task(encode_processing_task(invocation)))
    validate_processing_result(invocation, result)
    if consumption == "collective":
        assert len(result.groups) == 1
        assert result.groups[0].outputs == ()
        assert tuple(row.row_index for row in result.groups[0].consumed_rows) == ("sample-0", "sample-1", "sample-2")
    else:
        assert [group.outputs for group in result.groups] == [(), ({"value": 2},), ({"value": 3}, {"value": 4})]


def test_empty_batch_reads_admitted_constants_and_auxiliary_reference(tmp_path):
    from bioimageflow_core import ReferenceRow
    source = tmp_path / "tool.py"
    source.write_text("""
from bioimageflow_core import IOModel, ProcessingTool, RowConsumption
class ContextTool(ProcessingTool):
    row_consumption = RowConsumption.COLLECTIVE
    class Outputs(IOModel):
        value: int
        path: str
    def process_batch(self, arguments_list, *, context=None):
        assert arguments_list == []
        assert context.reference_rows[0].row_index == "reference-image"
        return [self.Outputs(value=context.batch_arguments.offset + context.reference_rows[0].arguments["pixels"],
                             path=str(context.batch_arguments.output))]
""")
    output = tmp_path / "aggregate.npy"
    invocation = ProcessingTask(
        task_id="task_" + "0" * 16, node_name="empty", invocation_id="inv_" + "1" * 32,
        cache_attempt_id=None, task_retry=0, mode="process_batch", row_consumption="collective",
        tool=_origin(source), declaration=_declaration(outputs={"value": int, "path": str}), rows=(), batch_context=_context((tmp_path / "run").resolve(), row_index=None),
        batch_arguments={"offset": 7, "output": output},
        reference_rows=(ReferenceRow(0, "reference-image", {"pixels": 11}),),
    )
    result = decode_processing_result(execute_processing_task(encode_processing_task(invocation)))
    validate_processing_result(invocation, result)
    assert result.groups[0].consumed_rows == ()
    assert result.groups[0].outputs == ({"value": 18, "path": str(output)},)
