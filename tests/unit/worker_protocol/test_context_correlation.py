"""Current public Task4 entry: context correlation precedes trusted import."""
from dataclasses import replace
import re

import pytest

from bioimageflow_core import decode_processing_result, encode_processing_task, validate_processing_result
from bioimageflow_core.worker import execute_processing_task
from bioimageflow_core.worker_origins import clear_worker_tool_instances
from tests.unit.worker_protocol.test_codecs import _task
from tests.unit.worker_protocol.test_execution import _context, _declaration, _origin


@pytest.mark.parametrize("variant", ["wrong-index", "row-batch-role", "batch-row-role", "matching-row", "matching-batch", "absent-row-context"])
def test_context_is_correlated_before_import_and_callback(tmp_path, variant):
    invocation = _task(tmp_path)
    source = tmp_path / "context_tool.py"
    imported = tmp_path / "imported"
    called = tmp_path / "called"
    source.write_text(f'''
from pathlib import Path
from bioimageflow_core import IOModel, ProcessingTool, RowConsumption
Path({str(imported)!r}).write_text("imported")
class ContextTool(ProcessingTool):
    row_consumption = RowConsumption.MAPPED
    class Inputs(IOModel):
        value: int
    class Outputs(IOModel):
        value: int
        seen: str
        role: str
    def process_row(self, arguments, *, context=None):
        Path({str(called)!r}).write_text("row")
        return self.Outputs(value=arguments.value, seen="none" if context is None else str(context.row_index), role="none" if context is None else "row" if context.row_dir is not None else "batch")
    def process_batch(self, arguments_list, *, context=None):
        Path({str(called)!r}).write_text("batch")
        return [self.Outputs(value=arguments.value, seen=str(context.row_index), role="row" if context.row_dir is not None else "batch") for arguments in arguments_list]
''')
    run = (tmp_path / "run").resolve()
    row_context = _context(run, row_index="sample")
    batch_context = None
    mode = "row_chunk"
    if variant == "wrong-index":
        row_context = _context(run, row_index="different")
    elif variant == "row-batch-role":
        row_context = _context(run, row_index=None)
    elif variant in ("batch-row-role", "matching-batch"):
        mode = "process_batch"
        batch_context = _context(run, row_index="different" if variant == "batch-row-role" else None)
    elif variant == "absent-row-context":
        row_context = None
    invocation = replace(invocation, tool=_origin(source), declaration=_declaration({"value": int}, {"value": int, "seen": str, "role": str}), mode=mode, rows=(replace(invocation.rows[0], context=row_context),), batch_context=batch_context)
    malformed = variant in ("wrong-index", "row-batch-role", "batch-row-role")
    clear_worker_tool_instances()
    try:
        if malformed:
            expected_path = {"wrong-index": "rows[0].context.row_index", "row-batch-role": "rows[0].context", "batch-row-role": "batch_context"}[variant]
            with pytest.raises(ValueError, match=re.escape(expected_path)):
                execute_processing_task(encode_processing_task(invocation))
            assert not imported.exists()
            assert not called.exists()
            return
        result = decode_processing_result(execute_processing_task(encode_processing_task(invocation)))
        validate_processing_result(invocation, result)
        assert imported.read_text() == "imported"
        assert called.read_text() == ("batch" if mode == "process_batch" else "row")
        assert [(row.position, row.row_index) for row in result.groups[0].consumed_rows] == [(0, "sample")]
        output = result.groups[0].outputs[0]
        assert output["value"] == 3
        expected = {"matching-row": ("sample", "row"), "matching-batch": ("None", "batch"), "absent-row-context": ("none", "none")}[variant]
        assert (output["seen"], output["role"]) == expected
    finally:
        clear_worker_tool_instances()
