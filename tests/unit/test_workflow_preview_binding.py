"""Preview values and addresses share one held canonical record admission."""

from pathlib import Path

import pytest

from bioimageflow import Workflow, WorkflowExecutionContext
from bioimageflow.storage import Storage
from docs.source.workflows.images import generate_workflow_previews as previews
from tests.testkit.runtime_cache import SourceAssetWriter


def test_preview_holds_record_a_when_latest_node_changes_to_b(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    outputs = tmp_path / "outputs"
    storage = Storage(outputs / "fixture")
    records = []
    for value in (1, 9):
        context = WorkflowExecutionContext()
        with Workflow(engine="direct", storage_path=storage.storage_path) as workflow:
            node = SourceAssetWriter()(text=str(value), name="node")
            public_frame = workflow.compute(node, run_context=context)
        [selected] = context.execution_outcomes
        records.append((context.run_id, selected))
    storage.update_latest_node("node", records[0][0])
    monkeypatch.setattr(previews, "OUTPUTS_ROOT", outputs)
    original_select = Storage.read_latest_node_result
    original_load = Storage.load_record
    selections = []
    admissions = []

    def select_then_change(self, node_key):
        selected = original_select(self, node_key)
        selections.append(selected)
        storage.update_latest_node("node", records[1][0])
        return selected

    def exact_load(self, result_key, record_id, **kwargs):
        admissions.append((result_key, record_id))
        return original_load(self, result_key, record_id, **kwargs)

    monkeypatch.setattr(Storage, "read_latest_node_result", select_then_change)
    monkeypatch.setattr(Storage, "load_record", exact_load)
    artifacts = previews.WorkflowArtifacts("fixture")

    path = artifacts.path_from_column("node", "mask")

    assert path.read_text() == "1"
    first = records[0][1]
    assert len(selections) == 1
    assert selections[0].run_id == records[0][0]
    assert admissions == [(first.result_key, first.record_id)]
    first_directory = storage.result_dir(first.result_key) / "records" / first.record_id
    assert path.parent == first_directory / "assets"
    manifest, frame, directory = artifacts.load_record("node", path_columns=("mask",))
    second = records[1][1]
    assert manifest.record_id == second.record_id
    assert directory == storage.result_dir(second.result_key) / "records" / second.record_id
    assert frame.index.tolist() == public_frame.index.tolist()
    assert frame["count"].tolist() == [1]
    stored_path = Path(frame.iloc[0]["mask"])
    assert not stored_path.is_absolute()
    second_path = directory / stored_path
    assert second_path.read_text() == "9"
    assert second_path.parent == directory / "assets"
    assert second_path.name == path.name
    assert second_path != path
    assert admissions[-1] == (second.result_key, second.record_id)
    assert path.read_text() == "1"
