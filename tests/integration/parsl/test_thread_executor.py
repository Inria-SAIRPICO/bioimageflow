"""Real attached Parsl control-plane and dispatch coverage."""

from __future__ import annotations

import json
from threading import Event, Thread
from types import MethodType

import pandas as pd
import pytest
from parsl import Config
from parsl.executors.threads import ThreadPoolExecutor

from bioimageflow import (
    DataFrameTool,
    ExecutorBinding,
    ExecutorCapabilities,
    ParslEngine,
    ParslTaskPolicy,
    WorkerEnvironmentAttestation,
    WorkerSlotCapacity,
    Workflow,
    WorkflowCancelledError,
)
from bioimageflow.cache import compute_env_hash
from bioimageflow.parsl.startup import CORE_REQUIREMENT
from bioimageflow.storage import Storage
from bioimageflow_core import Arguments, IOModel
from tests.testkit.parsl_tools import (
    PARSL_TEST_ENV,
    ParslBatch,
    ParslDelayed,
    ParslEmptyBatch,
    ParslFail,
    ParslIncrement,
)


class _Rows(DataFrameTool):
    class Inputs(IOModel):
        values: tuple[int, ...]

    class Outputs(IOModel):
        value: int

    def transform(self, df: pd.DataFrame, arguments: Arguments) -> pd.DataFrame:
        return pd.DataFrame(
            {"value": list(arguments.values)},
            index=[f"row-{index}" for index in range(len(arguments.values))],
        )


class _BlockingRows(DataFrameTool):
    entered = Event()
    release = Event()

    class Inputs(IOModel):
        value: int

    class Outputs(IOModel):
        value: int

    def transform(self, df: pd.DataFrame, arguments: Arguments) -> pd.DataFrame:
        type(self).entered.set()
        assert type(self).release.wait(timeout=5)
        return pd.DataFrame({"value": [arguments.value]}, index=["row"])


def _binding(label: str = "threads") -> ExecutorBinding:
    return ExecutorBinding(
        label=label,
        environments=(
            WorkerEnvironmentAttestation(
                name=PARSL_TEST_ENV.name,
                dependency_hash=compute_env_hash(PARSL_TEST_ENV.dependencies),
                allow_flexible_versions=False,
                core_requirement=CORE_REQUIREMENT,
            ),
        ),
        capabilities=ExecutorCapabilities(
            storage_modes=("shared_fs",),
            tool_origin_modes=("shared_module",),
            slot=WorkerSlotCapacity(cpu=1),
        ),
    )


def _workflow(storage_path, events=None) -> tuple[Workflow, object]:
    workflow = Workflow(
        storage_path=storage_path,
        engine="direct",
        on_progress=None if events is None else events.append,
    )
    with workflow:
        node = ParslIncrement()(value=4)
    return workflow, node


def _thread_config(tmp_path, *, max_threads: int = 2) -> Config:
    return Config(
        executors=[
            ThreadPoolExecutor(
                label="threads",
                max_threads=max_threads,
            )
        ],
        retries=0,
        run_dir=str(tmp_path / "runinfo"),
        usage_tracking=0,
    )


@pytest.mark.parsl
def test_real_thread_executor_matches_direct_output_and_cache_identity(
    tmp_path,
) -> None:
    direct_workflow, direct_node = _workflow(tmp_path / "direct")
    direct = direct_workflow.compute(direct_node)
    direct_key = direct_workflow.plan()[direct_node.name].final_result_key

    events = []
    parsl_workflow, parsl_node = _workflow(tmp_path / "parsl", events)
    engine = ParslEngine(
        parsl_config=_thread_config(tmp_path),
        executor_bindings={"threads": _binding()},
    )
    remote = parsl_workflow.compute(parsl_node, engine=engine)
    remote_key = parsl_workflow.plan()[parsl_node.name].final_result_key

    pd.testing.assert_frame_equal(remote, direct)
    assert remote_key == direct_key
    assert [event.status for event in events] == [
        "started",
        "row_complete",
        "completed",
    ]
    assert events[1].row == 0
    assert events[1].total_rows == 1
    storage = Storage(tmp_path / "parsl")
    run_id = storage.latest_success_run_id()
    assert run_id is not None
    run = json.loads((storage.run_dir(run_id) / "run.json").read_text())
    assert run["engine"] == "parsl:parallel"
    [task_path] = list(
        (
            storage.storage_path
            / "diagnostics"
            / "v1"
            / "runs"
            / run_id
        ).rglob("task_*.json")
    )
    diagnostic = json.loads(task_path.read_text())
    assert diagnostic["status"] == "succeeded"
    assert diagnostic["executor_label"] == "threads"
    assert diagnostic["cache_attempt_id"] is not None
    assert diagnostic["completed_at"] is not None


@pytest.mark.parsl
def test_fully_cached_attached_run_does_not_create_a_dfk(
    tmp_path,
) -> None:
    workflow, node = _workflow(tmp_path)
    workflow.compute(node)

    class NoStartEngine(ParslEngine):
        def _start_attached_execution(self):
            raise AssertionError("a fully cached run must not acquire a DFK")

    engine = NoStartEngine(
        parsl_config=_thread_config(tmp_path),
        executor_bindings={"threads": _binding()},
    )

    result = workflow.compute(node, engine=engine)

    assert result.loc["0", "value"] == 5


@pytest.mark.parsl
def test_disappearing_planned_cache_selection_never_falls_back_to_direct(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow, node = _workflow(tmp_path)
    workflow.compute(node)
    result_key = workflow.plan()[node.name].final_result_key
    assert result_key is not None
    current_path = (
        workflow.storage_path
        / "cache"
        / "v1"
        / "results"
        / result_key[3:5]
        / result_key[5:7]
        / result_key
        / "current.json"
    )
    engine = ParslEngine(
        parsl_config=_thread_config(tmp_path),
        executor_bindings={"threads": _binding()},
    )
    original_execute = ParslEngine._execute_attached

    def remove_selection_then_execute(self, targets, selected_workflow, dfk):
        current_path.unlink()
        return original_execute(self, targets, selected_workflow, dfk)

    engine._execute_attached = MethodType(remove_selection_then_execute, engine)
    monkeypatch.setattr(
        ParslIncrement,
        "process_row",
        lambda *_args, **_kwargs: pytest.fail("tool executed in direct mode"),
    )

    with pytest.raises(RuntimeError, match="cache selection.*changed"):
        workflow.compute(node, engine=engine)

    assert engine.dfk is None


@pytest.mark.parsl
def test_real_thread_executor_chunks_preserve_input_and_progress_order(
    tmp_path,
) -> None:
    events = []
    workflow = Workflow(
        storage_path=tmp_path,
        engine="direct",
        on_progress=events.append,
    )
    with workflow:
        rows = _Rows()(values=(3, 2, 1, 0))
        delayed = ParslDelayed()(value=rows["value"])
    engine = ParslEngine(
        parsl_config=_thread_config(tmp_path),
        executor_bindings={"threads": _binding()},
        task_policy=ParslTaskPolicy(row_chunk_size=2, max_in_flight=2),
    )

    result = workflow.compute(delayed, engine=engine)

    assert list(result.index) == ["row-0", "row-1", "row-2", "row-3"]
    assert list(result["value"]) == [30, 20, 10, 0]
    row_events = [event for event in events if event.status == "row_complete"]
    assert [event.row for event in row_events] == [0, 1, 2, 3]


@pytest.mark.parsl
def test_real_thread_executor_runs_batch_as_one_task_without_row_events(
    tmp_path,
) -> None:
    events = []
    workflow = Workflow(
        storage_path=tmp_path,
        engine="direct",
        on_progress=events.append,
    )
    with workflow:
        rows = _Rows()(values=(2, 4))
        batch = ParslBatch()(value=rows["value"])
    engine = ParslEngine(
        parsl_config=_thread_config(tmp_path),
        executor_bindings={"threads": _binding()},
    )

    result = workflow.compute(batch, engine=engine)

    assert list(result.index) == [
        "row-0::0",
        "row-0::1",
        "row-1::0",
        "row-1::1",
    ]
    assert list(result["value"]) == [2, 102, 4, 104]
    assert all(event.status != "row_complete" for event in events)


@pytest.mark.parsl
def test_real_thread_executor_error_retains_full_task_correlation(
    tmp_path,
) -> None:
    from bioimageflow import ParslTaskError

    workflow = Workflow(storage_path=tmp_path, engine="direct")
    with workflow:
        failed = ParslFail()(value=7)
    engine = ParslEngine(
        parsl_config=_thread_config(tmp_path),
        executor_bindings={"threads": _binding()},
    )

    with pytest.raises(ParslTaskError) as captured:
        workflow.compute(failed, engine=engine)

    error = captured.value
    assert error.scoped_node_name == failed.name
    assert error.executor_label == "threads"
    assert error.task_id == "task_0000000000000000"
    assert error.invocation_id.startswith("inv_")
    assert error.cache_attempt_id is not None
    assert error.task_retry == 0
    assert error.row_position == 0
    assert error.original_type == "RuntimeError"
    assert "remote failure 7" in error.original_message
    assert "remote failure 7" in (error.remote_traceback or "")
    assert workflow.plan()[failed.name].selected_record_id is None
    [task_path] = list(
        (tmp_path / "diagnostics" / "v1" / "runs").rglob("task_*.json")
    )
    diagnostic = json.loads(task_path.read_text())
    assert diagnostic["status"] == "failed"
    assert diagnostic["error_type"] == "RuntimeError"
    [attempt_path] = list(
        (tmp_path / "cache" / "v1" / "results").rglob("attempt.json")
    )
    attempt = json.loads(attempt_path.read_text())
    assert attempt["status"] == "failed"
    assert attempt["run_id"] == diagnostic["run_id"]
    assert attempt["invocation_id"] == diagnostic["invocation_id"]
    assert attempt["engine"] == "parsl:parallel"


@pytest.mark.parsl
def test_real_thread_executor_compute_steps_uses_attached_lifecycle(
    tmp_path,
) -> None:
    workflow, node = _workflow(tmp_path)
    engine = ParslEngine(
        parsl_config=_thread_config(tmp_path),
        executor_bindings={"threads": _binding()},
    )

    steps = workflow.compute_steps(node, engine=engine)
    step = next(steps)
    result = step.execute()
    with pytest.raises(StopIteration):
        next(steps)

    assert result.loc["0", "value"] == 5
    assert engine.dfk is None


@pytest.mark.parsl
def test_close_releases_unconsumed_public_steps_reservation(
    tmp_path,
) -> None:
    workflow, node = _workflow(tmp_path)
    engine = ParslEngine(
        parsl_config=_thread_config(tmp_path),
        executor_bindings={"threads": _binding()},
    )
    steps = workflow.compute_steps(node, engine=engine)

    thread = Thread(target=engine.close)
    thread.start()
    thread.join(timeout=5)

    assert not thread.is_alive()
    assert engine.dfk is None
    steps.close()


@pytest.mark.parsl
def test_real_thread_executor_empty_batch_contract(
    tmp_path,
) -> None:
    workflow = Workflow(storage_path=tmp_path, engine="direct")
    with workflow:
        rows = _Rows()(values=())
        skipped = ParslBatch()(value=rows["value"], name="skipped")
        executed = ParslEmptyBatch()(value=rows["value"], name="executed")
    engine = ParslEngine(
        parsl_config=_thread_config(tmp_path),
        executor_bindings={"threads": _binding()},
        resource_lifetime="engine",
    )
    try:
        skipped_result = workflow.compute(skipped, engine=engine)
        executed_result = workflow.compute(executed, engine=engine)
    finally:
        engine.close()

    assert skipped_result.empty
    assert list(skipped_result.columns) == ["value"]
    assert executed_result.loc["0", "count"] == 0


@pytest.mark.parsl
def test_recursive_processing_dispatches_with_scoped_name_and_local_boundary(
    tmp_path,
) -> None:
    child = Workflow(
        name="increment-child",
        storage_path=tmp_path,
        engine="direct",
    )
    with child:
        value = child.input("value", int, id="input-value")
        incremented = ParslIncrement()(value=value, name="increment")
        child.output(
            "result",
            incremented["value"],
            id="output-result",
        )
    events = []
    parent = Workflow(
        name="parent",
        storage_path=tmp_path,
        engine="direct",
        on_progress=events.append,
    )
    with parent:
        nested = child(value=9, name="nested")
    engine = ParslEngine(
        parsl_config=_thread_config(tmp_path),
        executor_bindings={"threads": _binding()},
    )

    result = parent.compute(nested, engine=engine)

    assert result.loc["0", "result"] == 10
    assert {event.node_name for event in events} == {"nested/increment"}
    assert parent.plan()["nested"].final_result_key is None


@pytest.mark.parsl
def test_cancellation_after_local_dataframe_work_prevents_publication(
    tmp_path,
) -> None:
    _BlockingRows.entered.clear()
    _BlockingRows.release.clear()
    workflow = Workflow(storage_path=tmp_path, engine="direct")
    with workflow:
        node = _BlockingRows()(value=8)
    engine = ParslEngine(
        parsl_config=_thread_config(tmp_path),
        executor_bindings={"threads": _binding()},
    )
    failures: list[BaseException] = []

    def compute() -> None:
        try:
            workflow.compute(node, engine=engine)
        except BaseException as exc:
            failures.append(exc)

    thread = Thread(target=compute)
    thread.start()
    assert _BlockingRows.entered.wait(timeout=5)
    workflow.cancel()
    _BlockingRows.release.set()
    thread.join(timeout=5)

    assert not thread.is_alive()
    assert len(failures) == 1
    assert isinstance(failures[0], WorkflowCancelledError)
    assert workflow.plan()[node.name].selected_record_id is None
    runs = list((tmp_path / "views" / "runs").glob("run_*"))
    assert len(runs) == 1
    metadata = json.loads((runs[0] / "run.json").read_text())
    assert metadata["status"] == "cancelled"
    assert not (runs[0] / "nodes" / node.name).exists()
