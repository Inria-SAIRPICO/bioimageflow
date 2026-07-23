"""Real attached Parsl control-plane and dispatch coverage."""

from __future__ import annotations

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
)
from bioimageflow.cache import compute_env_hash
from bioimageflow_core import Arguments, IOModel
from tests.testkit.parsl_tools import (
    PARSL_TEST_ENV,
    ParslBatch,
    ParslDelayed,
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


def _binding(label: str = "threads") -> ExecutorBinding:
    return ExecutorBinding(
        label=label,
        environments=(
            WorkerEnvironmentAttestation(
                name=PARSL_TEST_ENV.name,
                dependency_hash=compute_env_hash(PARSL_TEST_ENV.dependencies),
                allow_flexible_versions=False,
                core_requirement="bioimageflow-core>=0.1.7,<0.2",
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
