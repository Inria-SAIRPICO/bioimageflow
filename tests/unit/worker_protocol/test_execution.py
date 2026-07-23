"""Canonical processing entry-point tests."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from bioimageflow_core import (
    ProcessingTaskV1,
    RowInvocationV1,
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
from bioimageflow_core import Arguments, ExecutionContext, IOModel, ProcessingTool

class ContextTool(ProcessingTool):
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
    invocation = ProcessingTaskV1(
        task_id="task_0000000000000000",
        node_name="context",
        invocation_id=f"inv_{'1' * 32}",
        cache_attempt_id=None,
        task_retry=0,
        mode="row_chunk",
        tool=_origin(source),
        rows=(
            RowInvocationV1(
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
    assert result.rows[0].outputs == (
        {"seen": str(run_dir / "work" / "rows" / "000000" / "marker")},
    )


def test_batch_one_to_one_shorthand_is_normalized(tmp_path) -> None:
    source = tmp_path / "context_tool.py"
    source.write_text(
        """
from bioimageflow_core import Arguments, ExecutionContext, IOModel, ProcessingTool

class ContextTool(ProcessingTool):
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
    invocation = ProcessingTaskV1(
        task_id="task_0000000000000000",
        node_name="context",
        invocation_id=f"inv_{'1' * 32}",
        cache_attempt_id=f"att_{'2' * 32}",
        task_retry=0,
        mode="process_batch",
        tool=_origin(source),
        rows=tuple(
            RowInvocationV1(
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
    assert [row.outputs for row in result.rows] == [
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
    invocation = ProcessingTaskV1(
        task_id="task_0000000000000000",
        node_name="context",
        invocation_id=f"inv_{'1' * 32}",
        cache_attempt_id=None,
        task_retry=0,
        mode="row_chunk",
        tool=_origin(source),
        rows=(),
    )
    payload = encode_processing_task(invocation)
    payload["future"] = True
    with pytest.raises(ValueError):
        execute_processing_task(payload)
    assert not marker.exists()
