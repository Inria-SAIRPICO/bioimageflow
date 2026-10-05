"""Actual public computation and cache selection retain executable authority."""

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys
from types import ModuleType

import pytest

from bioimageflow import Workflow, WorkflowExecutionContext
from bioimageflow.storage import Storage


def _compute(workflow, node=None):
    context = WorkflowExecutionContext()
    frame = workflow.compute(run_context=context) if node is None else workflow.compute(node, run_context=context)
    [outcome] = context.execution_outcomes
    assert outcome.result_key is not None and outcome.record_id is not None
    storage = Storage(workflow.storage_path)
    pointer = storage.load_current(outcome.result_key)
    assert pointer is not None and pointer.record_id == outcome.record_id
    manifest = storage.load_record_manifest(outcome.result_key, outcome.record_id)
    assert manifest is not None
    return frame["value"].tolist(), (outcome.result_key, outcome.record_id)


def test_same_id_single_file_archives_keep_a_b_a_execution_and_records(tmp_path):
    fixture = json.loads(Path("tests/fixtures/unified_workflow_archive.json").read_text())
    archive_a = {
        "archive_version": fixture["archive_version"],
        "workflow": deepcopy(fixture["workflow"]["nodes"][0]["workflow"]),
        "custom_sources": [deepcopy(fixture["custom_sources"][0])],
    }
    record_a = archive_a["custom_sources"][0]
    record_a["id"] = "single-" + tmp_path.name
    archive_a["workflow"]["nodes"][0]["source_module"] = record_a["id"]
    source_a = record_a["source"].replace(
        "    accepts_upstream = False\n", "    accepts_upstream = False\n    calls = []\n"
    ).replace("        import pandas as pd\n", "        self.calls.append(1)\n        import pandas as pd\n")
    record_a.update(source=source_a, source_hash=hashlib.sha256(source_a.encode()).hexdigest())
    archive_b = deepcopy(archive_a)
    source_b = source_a.replace("self.calls.append(1)", "self.calls.append(9)").replace("[1]", "[9]")
    archive_b["custom_sources"][0].update(
        source=source_b, source_hash=hashlib.sha256(source_b.encode()).hexdigest()
    )
    storage = tmp_path / "records"
    workflow_a = Workflow.from_dict(archive_a, storage_path=storage, engine="direct")
    tool_a = workflow_a.nodes["collision"].tool
    values_a, selected_a = _compute(workflow_a)
    workflow_b = Workflow.from_dict(archive_b, storage_path=storage, engine="direct")
    tool_b = workflow_b.nodes["collision"].tool
    values_b, selected_b = _compute(workflow_b)
    values_a_again, selected_a_again = _compute(workflow_a)
    assert values_a == values_a_again == [1]
    assert values_b == [9]
    assert selected_a_again == selected_a
    assert selected_a[0] != selected_b[0]
    assert tool_a.calls == [1] and tool_b.calls == [9]
    exported_a = workflow_a.to_archive_dict()
    [exported_source_a] = exported_a["custom_sources"]
    assert exported_source_a["source"] == source_a, "retained A must not export B's resident module bytes"
    assert exported_source_a["source_hash"] == record_a["source_hash"]


@pytest.mark.parametrize("change", ["body", "literal_global", "same_source_helper"])
def test_same_path_fixed_version_direct_body_change_does_not_reuse_old_record(
    tmp_path, monkeypatch: pytest.MonkeyPatch, change,
):
    module_name = "direct_source_" + tmp_path.name.replace("-", "_")
    source_path = tmp_path / "tool.py"

    def load(value):
        support = {
            "body": "",
            "literal_global": f"VALUE = {value}\n",
            "same_source_helper": f"def actual_value():\n    return {value}\n",
        }[change]
        expression = {"body": str(value), "literal_global": "VALUE", "same_source_helper": "actual_value()"}[change]
        source = f'''from bioimageflow import DataFrameTool
from bioimageflow_core import IOModel
{support}
class SourceTool(DataFrameTool):
    accepts_upstream = False
    _bif_package_version = "1.0.0"
    calls = []
    class Inputs(IOModel): pass
    class Outputs(IOModel): value: int
    def transform(self, df, arguments):
        value = {expression}
        self.calls.append(value)
        import pandas as pd
        return pd.DataFrame({{"value": [value]}}, index=["row"])
'''
        source_path.write_text(source)
        module = ModuleType(module_name)
        module.__file__ = str(source_path)
        monkeypatch.setitem(sys.modules, module_name, module)
        exec(compile(source, str(source_path), "exec"), module.__dict__)
        return module.SourceTool

    type_a = load(1)
    storage = tmp_path / "records"
    with Workflow(engine="direct", storage_path=storage) as workflow_a:
        node_a = type_a()(name="source")
    values_a, selected_a = _compute(workflow_a, node_a)
    type_b = load(9)
    with Workflow(engine="direct", storage_path=storage) as workflow_b:
        node_b = type_b()(name="source")
    values_b, selected_b = _compute(workflow_b, node_b)
    assert values_a == [1]
    assert values_b == [9], "new actual B computation must not return the cached A frame"
    assert selected_a[0] != selected_b[0]
    assert type_a.calls == [1] and type_b.calls == [9]
    values_b_again, selected_b_again = _compute(workflow_b, node_b)
    assert values_b_again == [9] and selected_b_again == selected_b
    assert type_b.calls == [9], "unchanged admitted B must reuse its own record"
    values_a_again, selected_a_again = _compute(workflow_a, node_a)
    assert values_a_again == [1] and selected_a_again == selected_a
    assert type_a.calls == [1], "unchanged admitted A must still reuse its own record"
