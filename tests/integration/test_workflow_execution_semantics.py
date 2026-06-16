"""Focused workflow execution semantics used by platform callers."""

from pathlib import Path
from typing import Annotated, Any

import pandas as pd
import pytest

from bioimageflow import NodePlanStatus, SubWorkflow, Workflow
from bioimageflow.cache import dataframe_v1_result_key, processing_v1_result_key
from bioimageflow.engine import WorkflowCancelledError
from bioimageflow.storage import get_node_dir
from bioimageflow.storage_v1 import StorageV1
from bioimageflow_core import Arguments, IOModel, ImageSpec, ProcessingTool, Semantic

from .conftest import AddColumn, FileLoader, StubSegmenter, imageio_env


def _processing_v1_current_exists(storage: Path, node_name: str, sig_hash: str) -> bool:
    result_key = processing_v1_result_key(node_name, sig_hash)
    return (StorageV1(storage).result_dir(result_key) / "current.json").exists()


def _build_loader_and_tagged(
    storage_path: Path,
    data_path: Path,
    *,
    on_progress: Any | None = None,
) -> tuple[Workflow, Any]:
    wf = Workflow(
        storage_path=storage_path,
        on_progress=on_progress,
    )
    with wf:
        raw = FileLoader()(path=str(data_path))
        tagged = AddColumn()(raw, column_name="tag", value="library")
    return wf, tagged


class TestDataFrameToolPlanCacheParity:
    def test_dataframe_tool_compute_materializes_cache_and_plan_reports_cached(
        self, tmp_workspace: Path,
    ) -> None:
        storage = tmp_workspace / "results"

        wf1, tagged1 = _build_loader_and_tagged(storage, tmp_workspace / "data")
        pre_plan = wf1.plan()
        assert pre_plan["FileLoader_1"].status is NodePlanStatus.UNEXECUTED
        assert pre_plan["AddColumn_1"].status is NodePlanStatus.UNEXECUTED

        df1 = wf1.compute(tagged1)
        assert set(df1["tag"]) == {"library"}

        wf2, _tagged2 = _build_loader_and_tagged(storage, tmp_workspace / "data")
        cached_plan = wf2.plan()
        assert cached_plan["FileLoader_1"].status is NodePlanStatus.CACHED
        assert cached_plan["AddColumn_1"].status is NodePlanStatus.CACHED

        v1_storage = StorageV1(storage)
        for node_name, entry in cached_plan.items():
            result_key = dataframe_v1_result_key(node_name, entry.sig_hash)
            pointer = v1_storage.load_current(result_key)
            assert pointer is not None
            record_dir = v1_storage.result_dir(result_key) / "records" / pointer.record_id
            assert (record_dir / "dataframe.parquet").exists()
            assert (record_dir / "manifest.json").exists()
        assert not (storage / "data" / "FileLoader_1").exists()
        assert not (storage / "data" / "AddColumn_1").exists()

        events = []
        wf3, tagged3 = _build_loader_and_tagged(
            storage,
            tmp_workspace / "data",
            on_progress=lambda e: events.append(e),
        )
        df2 = wf3.compute(tagged3)

        comparable1 = df1.copy()
        comparable2 = df2.copy()
        comparable1["path"] = comparable1["path"].astype(str)
        comparable2["path"] = comparable2["path"].astype(str)
        pd.testing.assert_frame_equal(comparable1, comparable2)
        cached_nodes = {e.node_name for e in events if e.status == "cached"}
        assert cached_nodes == {"FileLoader_1", "AddColumn_1"}


class TestTargetExecution:
    def test_compute_target_executes_only_reachable_upstream_dependencies(
        self, tmp_workspace: Path,
    ) -> None:
        storage = tmp_workspace / "results"
        events = []

        with Workflow(
            storage_path=storage,
            on_progress=lambda e: events.append(e),
        ) as wf:
            raw = FileLoader()(path=str(tmp_workspace / "data"))
            selected = StubSegmenter()(
                input_image=raw["path"],
                diameter=20.0,
                name="selected",
            )
            _unselected = StubSegmenter()(
                input_image=raw["path"],
                diameter=40.0,
                name="unselected",
            )
            df = wf.compute(selected)

        assert len(df) == 3
        event_nodes = {e.node_name for e in events}
        assert "FileLoader_1" in event_nodes
        assert "selected" in event_nodes
        assert "unselected" not in event_nodes
        with Workflow(storage_path=storage) as wf:
            raw = FileLoader()(path=str(tmp_workspace / "data"))
            selected = StubSegmenter()(
                input_image=raw["path"],
                diameter=20.0,
                name="selected",
            )
            unselected = StubSegmenter()(
                input_image=raw["path"],
                diameter=40.0,
                name="unselected",
            )
            plan = wf.plan()
        assert _processing_v1_current_exists(storage, selected.name, plan[selected.name].sig_hash)
        assert not _processing_v1_current_exists(storage, unselected.name, plan[unselected.name].sig_hash)


class TestFailureAndCancellation:
    def test_failed_downstream_node_keeps_completed_upstream_cache(
        self, tmp_workspace: Path,
    ) -> None:
        class FailingConsumer(ProcessingTool):
            display_name = "Failing Consumer"
            environment = imageio_env

            class Inputs(IOModel):
                mask: Annotated[Path, ImageSpec(semantics={Semantic.LABEL})]

            class Outputs(IOModel):
                result: int

            def process_row(self, arguments: Arguments, *, context: object | None = None) -> Any:
                raise RuntimeError(f"boom for {arguments.mask}")

        storage = tmp_workspace / "results"
        events = []

        with pytest.raises(RuntimeError, match="boom"):
            with Workflow(
                storage_path=storage,
                on_progress=lambda e: events.append(e),
            ) as wf:
                raw = FileLoader()(path=str(tmp_workspace / "data"))
                masks = StubSegmenter()(input_image=raw["path"])
                failed = FailingConsumer()(mask=masks["mask"])
                wf.compute(failed)

        assert any(
            e.node_name == "FailingConsumer_1" and e.status == "failed"
            for e in events
        )

        with Workflow(storage_path=storage) as wf:
            raw = FileLoader()(path=str(tmp_workspace / "data"))
            masks = StubSegmenter()(input_image=raw["path"])
            failed = FailingConsumer()(mask=masks["mask"])
            plan = wf.plan()

        assert failed.name == "FailingConsumer_1"
        assert plan["FileLoader_1"].status is NodePlanStatus.CACHED
        assert plan["StubSegmenter_1"].status is NodePlanStatus.CACHED
        assert plan["FailingConsumer_1"].status is NodePlanStatus.UNEXECUTED
        failed_dir = get_node_dir(storage, "FailingConsumer_1")
        assert not list(failed_dir.glob("*/dataframe.parquet"))
        assert not list(failed_dir.glob("*/dataframe.csv"))

        cached_events = []
        with Workflow(
            storage_path=storage,
            on_progress=lambda e: cached_events.append(e),
        ) as wf:
            raw = FileLoader()(path=str(tmp_workspace / "data"))
            masks = StubSegmenter()(input_image=raw["path"])
            wf.compute(masks)

        assert {
            e.node_name for e in cached_events if e.status == "cached"
        } == {"FileLoader_1", "StubSegmenter_1"}

    def test_in_flight_node_cancellation_keeps_cache_and_does_not_commit_output(
        self, tmp_workspace: Path,
    ) -> None:
        class CancellingSegmenter(ProcessingTool):
            display_name = "Cancelling Segmenter"
            environment = imageio_env

            class Inputs(IOModel):
                input_image: Annotated[Path, ImageSpec(semantics={Semantic.INTENSITY})]

            class Outputs(IOModel):
                mask: Annotated[Path, ImageSpec(semantics={Semantic.LABEL})]

            def process_row(self, arguments: Arguments, *, context: object | None = None) -> Any:
                Path(arguments.mask).parent.mkdir(parents=True, exist_ok=True)
                Path(arguments.mask).write_text("partial")
                raise WorkflowCancelledError("cancelled inside row")

        storage = tmp_workspace / "results"
        events = []

        with pytest.raises(WorkflowCancelledError, match="cancelled"):
            with Workflow(
                storage_path=storage,
                on_progress=lambda e: events.append(e),
            ) as wf:
                raw = FileLoader()(path=str(tmp_workspace / "data"))
                masks = CancellingSegmenter()(input_image=raw["path"])
                wf.compute(masks)

        assert any(
            e.node_name == "CancellingSegmenter_1" and e.status == "cancelled"
            for e in events
        )

        with Workflow(storage_path=storage) as wf:
            raw = FileLoader()(path=str(tmp_workspace / "data"))
            masks = CancellingSegmenter()(input_image=raw["path"])
            plan = wf.plan()

        assert masks.name == "CancellingSegmenter_1"
        assert plan["FileLoader_1"].status is NodePlanStatus.CACHED
        assert plan["CancellingSegmenter_1"].status is NodePlanStatus.UNEXECUTED
        cancelled_dir = get_node_dir(storage, "CancellingSegmenter_1")
        assert not list(cancelled_dir.glob("*/dataframe.parquet"))
        assert not list(cancelled_dir.glob("*/dataframe.csv"))


class TestSubWorkflowPlanCache:
    def test_sub_workflow_plan_uses_scoped_internal_cache_entries(
        self, tmp_workspace: Path,
    ) -> None:
        class SegmentOnly(SubWorkflow):
            display_name = "Segment Only"

            class Inputs(IOModel):
                image: Annotated[Path, ImageSpec(semantics={Semantic.INTENSITY})]

            class Outputs(IOModel):
                mask: Annotated[Path, ImageSpec(semantics={Semantic.LABEL})]
                cell_count: int

            def build(self, inputs: Any) -> dict[str, Any]:
                masks = StubSegmenter()(input_image=inputs.image)
                return {
                    "mask": masks["mask"],
                    "cell_count": masks["cell_count"],
                }

        storage = tmp_workspace / "results"

        with Workflow(storage_path=storage) as wf:
            raw = FileLoader()(path=str(tmp_workspace / "data"))
            results = SegmentOnly()(image=raw["path"])
            wf.compute(results)

        events = []
        with Workflow(
            storage_path=storage,
            on_progress=lambda e: events.append(e),
        ) as wf:
            raw = FileLoader()(path=str(tmp_workspace / "data"))
            results = SegmentOnly()(image=raw["path"])
            plan = wf.plan()
            df = wf.compute(results)

        internal_name = "SegmentOnly_1/StubSegmenter_1"
        assert len(df) == 3
        assert internal_name in plan
        assert plan[internal_name].status is NodePlanStatus.CACHED
        assert plan["SegmentOnly_1"].status is NodePlanStatus.CACHED
        assert _processing_v1_current_exists(
            storage,
            internal_name,
            plan[internal_name].sig_hash,
        )
        assert any(
            e.node_name == internal_name and e.status == "cached"
            for e in events
        )
