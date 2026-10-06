"""Direct identity retains both supported scientific callbacks."""

from bioimageflow import Workflow, WorkflowExecutionContext
from bioimageflow.storage import Storage
from bioimageflow_common_tools import Generate
from bioimageflow_core import GENERAL_ENV, IOModel, ProcessingTool, RowConsumption


def _make_tool(factor, audit):
    class Tool(ProcessingTool):
        row_consumption = RowConsumption.MAPPED
        environment = GENERAL_ENV

        class Inputs(IOModel):
            value: int

        class Outputs(IOModel):
            value: int

        def process_row(self, arguments):
            audit.append(factor)
            return self.Outputs(value=arguments.value * factor)

        def process_batch(self, arguments_list):
            return [self.process_row(arguments) for arguments in arguments_list]

    return Tool()


def _compute(tool, storage_path):
    context = WorkflowExecutionContext()
    with Workflow(engine="direct", storage_path=storage_path) as workflow:
        source = Generate()(column_name="value", values=[1], name="source")
        node = tool(value=source["value"], name="delegate")
        frame = workflow.compute(node, run_context=context)
    return frame["value"].tolist(), Storage(storage_path).read_run_node_result(context.run_id, "delegate")


def test_direct_batch_delegate_row_closure_selects_its_own_record(tmp_path):
    audit = []
    first_tool, changed_tool = _make_tool(1, audit), _make_tool(9, audit)
    assert type(first_tool).__qualname__ == type(changed_tool).__qualname__
    assert type(first_tool).process_batch.__code__ is type(changed_tool).process_batch.__code__
    assert type(first_tool).process_batch.__closure__ is None
    storage_path = tmp_path / "shared-records"
    first, first_record = _compute(first_tool, storage_path)
    warm_first, warm_first_record = _compute(first_tool, storage_path)
    assert first == warm_first == [1]
    assert not first_record.cache_hit and warm_first_record.cache_hit
    assert (first_record.result_key, first_record.record_id) == (warm_first_record.result_key, warm_first_record.record_id)
    assert audit == [1]

    independent_audit = []
    independent, independent_record = _compute(_make_tool(9, independent_audit), tmp_path / "fresh-records")
    assert independent == [9] and independent_audit == [9]
    assert not independent_record.cache_hit
    changed, changed_record = _compute(changed_tool, storage_path)
    assert changed == [9], "the delegated row closure must not select factor 1's record"
    assert not changed_record.cache_hit
    assert changed_record.result_key != first_record.result_key
    assert changed_record.record_id != first_record.record_id
    assert changed_record.result_key == independent_record.result_key
    assert audit == [1, 9]
    warm_changed, warm_changed_record = _compute(changed_tool, storage_path)
    assert warm_changed == [9] and warm_changed_record.cache_hit
    assert (warm_changed_record.result_key, warm_changed_record.record_id) == (changed_record.result_key, changed_record.record_id)
    assert audit == [1, 9]
    original, original_record = _compute(first_tool, storage_path)
    assert original == [1] and original_record.cache_hit
    assert (original_record.result_key, original_record.record_id) == (first_record.result_key, first_record.record_id)
    assert audit == [1, 9]
