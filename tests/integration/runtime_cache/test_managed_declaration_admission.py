"""Controller declarations must be admitted before a worker computes."""

from __future__ import annotations

import sys
from types import ModuleType

import pytest

from bioimageflow.cache.identity import deterministic_serialize
from bioimageflow.worker_origins import capture_tool_executable
from bioimageflow_core import (
    ProcessingTask,
    RowInvocation,
    declaration_digest,
    decode_processing_result,
    describe_tool_declaration,
    encode_processing_task,
    validate_processing_result,
)
from bioimageflow_core.worker import execute_processing_task
from bioimageflow_core.worker_origins import clear_worker_tool_instances


def _source(output_type: str) -> str:
    # No postponed annotation strings: the scientific method consumes the
    # loaded instance's actual native declaration, without changing its code.
    return f'''from bioimageflow_core import EnvironmentSpec, IOModel, ProcessingTool, RowConsumption
class DeclarationTool(ProcessingTool):
    row_consumption = RowConsumption.MAPPED
    environment = EnvironmentSpec("declaration", {{}})
    class Inputs(IOModel): pass
    class Outputs(IOModel): value: {output_type}
    def process_row(self, arguments, *, context):
        context.row_dir.mkdir(parents=True, exist_ok=True)
        (context.row_dir / "scientific-method-ran").write_text("computed")
        output_type = self.Outputs._get_all_annotations()["value"]
        return self.Outputs(value=output_type(4))
'''


@pytest.mark.parametrize("disk_type", ["int", "float"], ids=["matching", "changed-output-type"])
def test_loaded_declaration_is_admitted_before_scientific_method(tmp_path, monkeypatch, disk_type):
    source_path = tmp_path / "declaration_tool.py"
    resident_source = _source("int")
    source_path.write_text(resident_source)
    module = ModuleType("resident_declaration_" + tmp_path.name.replace("-", "_"))
    module.__file__ = str(source_path)
    monkeypatch.setitem(sys.modules, module.__name__, module)
    exec(compile(resident_source, str(source_path), "exec", dont_inherit=True), module.__dict__)
    resident = module.DeclarationTool()
    assert resident.Outputs._get_all_annotations()["value"] is int
    source_path.write_text(_source(disk_type))
    capture = capture_tool_executable(resident, managed=True, canonicalize=deterministic_serialize)
    declaration = describe_tool_declaration(resident)
    assert capture.scientific_key["declaration"]["outputs"]["fields"]["value"]["type_spec"] == {"kind": "int"}
    run_dir = (tmp_path / "run").resolve()
    row_dir = run_dir / "work" / "rows" / "000000"
    task = ProcessingTask(
        task_id="task_" + "1" * 16,
        node_name="declaration",
        invocation_id="inv_" + "2" * 32,
        cache_attempt_id=None,
        task_retry=0,
        mode="row_chunk",
        row_consumption="mapped",
        declaration=declaration,
        tool=capture.worker_origin,
        rows=(RowInvocation(position=0, row_index="sample", arguments={}, context={
            "run_dir": str(run_dir), "assets_dir": str(run_dir / "assets"),
            "work_dir": str(run_dir / "work"), "rows_dir": str(row_dir.parent),
            "row_dir": str(row_dir), "batch_dir": None, "row_index": "sample",
        }),),
    )
    clear_worker_tool_instances()
    try:
        try:
            result = decode_processing_result(execute_processing_task(encode_processing_task(task)))
        except ValueError as error:
            assert disk_type == "float"
            assert "declaration" in str(error).lower()
            assert "outputs.fields.value.type_spec" in str(error)
            assert not (row_dir / "scientific-method-ran").exists()
            return
        validate_processing_result(task, result)
        assert result.declaration_digest == declaration_digest(declaration)
        value = result.groups[0].outputs[0]["value"]
        assert (row_dir / "scientific-method-ran").read_text() == "computed"
        assert value == 4
        if disk_type == "float":
            assert type(value) is float
            pytest.fail(
                "Worker computed native float 4.0 under the controller's integer declaration; "
                "the scientific-method sentinel exists and result correlation validated."
            )
        assert type(value) is int
    finally:
        clear_worker_tool_instances()
