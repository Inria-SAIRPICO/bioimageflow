"""Public attached export preserves root and genuinely nested provider routes."""

import json

import pytest

from bioimageflow import Workflow, WorkflowExecutionContext, result_groups
from bioimageflow_core import SharedMemoryContext
from bioimageflow_core.shm import open_shared_array
from bundle_export_test_tools import ExportArray


@pytest.mark.shared_memory
@pytest.mark.parametrize("nested", [False, True], ids=["root", "nested"])
def test_root_export_preserves_renamed_shared_array_provider(tmp_path, nested):
    owner = SharedMemoryContext(tmp_path / "compute-owner")
    workflow = Workflow(
        name="published-root",
        storage_path=tmp_path / "storage",
        engine="direct",
        shared_memory_context=owner,
    )
    if nested:
        inner = Workflow(name="inner", engine="direct")
        with inner:
            writer = ExportArray()(name="writer")
        inner.output("child_image", writer["image"], id="child-image")
        with workflow:
            invocation = inner(name="nested")
        workflow.output("renamed_image", invocation["child_image"], id="public-image")
    else:
        with workflow:
            writer = ExportArray()(name="writer")
        workflow.output("renamed_image", writer["image"], id="public-image")

    context = WorkflowExecutionContext(shared_memory_context=owner)
    values = []
    try:
        result = workflow.compute(run_context=context)
        values.append(result)
        assert result.columns.tolist() == ["renamed_image"]
        [outcome] = context.execution_outcomes
        assert outcome.node_key == ("nested/writer" if nested else "writer")
        assert outcome.shared_array_columns == ("image",)
        with open_shared_array(result.at["0", "renamed_image"]) as pixels:
            assert pixels.tolist() == [[11, 11, 11], [11, 11, 11]]
        del pixels

        destination = tmp_path / "export"
        exported = context.export_result(result, destination=destination)
        values.append(exported)
        assert exported.columns.tolist() == ["renamed_image"]
        with open_shared_array(exported.at["0", "renamed_image"]) as pixels:
            assert pixels.tolist() == [[11, 11, 11], [11, 11, 11]]
            assert not pixels.flags.writeable
        del pixels
        manifest = json.loads((destination / "manifest.json").read_text())
        [locator] = manifest["return_manifest"]["locators"]
        assert locator["kind"] == "record_asset"
        assert locator["record_id"] == outcome.record_id
        assert locator["result_key"] == outcome.result_key
    finally:
        for value in values:
            for group in result_groups(value):
                group.release()
            value.at["0", "renamed_image"].bound_owner.close()
        owner.close()
