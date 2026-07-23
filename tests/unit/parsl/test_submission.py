"""Bounded Parsl task packing and collection tests."""

from __future__ import annotations

import concurrent.futures
from collections.abc import Iterator
from threading import Event, Thread

import pytest

from bioimageflow import ParslTaskError, WorkflowCancelledError
from bioimageflow.parsl.submission import (
    BoundedParslCollector,
    effective_in_flight_limit,
    iter_row_tasks,
    make_batch_task,
    validate_task_runtime_values,
)
from bioimageflow_core import (
    ProcessingTaskResultV1,
    ProcessingTaskV1,
    RowInvocationV1,
    RowResultV1,
    SourceFileOriginV1,
    encode_processing_result,
)
from bioimageflow_core.types import SharedArray


ORIGIN = SourceFileOriginV1(
    path="/shared/tool.py",
    source_hash="a" * 64,
    class_name="Tool",
)
INVOCATION_ID = "inv_" + "1" * 32
ATTEMPT_ID = "att_" + "2" * 32


def _rows(count: int) -> Iterator[RowInvocationV1]:
    for position in range(count):
        yield RowInvocationV1(
            position=position,
            row_index=f"row-{position}",
            arguments={"value": position},
            context=None,
        )


def _result(task, *, value_offset: int = 0):
    return encode_processing_result(
        ProcessingTaskResultV1(
            task_id=task.task_id,
            node_name=task.node_name,
            invocation_id=task.invocation_id,
            cache_attempt_id=task.cache_attempt_id,
            task_retry=task.task_retry,
            mode=task.mode,
            rows=tuple(
                RowResultV1(
                    position=row.position,
                    row_index=row.row_index,
                    outputs=({"value": row.position + value_offset},),
                )
                for row in task.rows
            ),
        )
    )


def test_effective_in_flight_limit_applies_execution_and_resource_bounds() -> None:
    assert effective_in_flight_limit(32, 0, sequential=False) == 32
    assert effective_in_flight_limit(32, 5, sequential=False) == 5
    assert effective_in_flight_limit(32, 5, sequential=True) == 1
    with pytest.raises(ValueError):
        effective_in_flight_limit(True, 0, sequential=False)


def test_row_tasks_are_consecutive_explicit_chunks() -> None:
    tasks = list(
        iter_row_tasks(
            node_name="nested/Tool_1",
            invocation_id=INVOCATION_ID,
            cache_attempt_id=ATTEMPT_ID,
            tool=ORIGIN,
            rows=_rows(5),
            row_chunk_size=2,
        )
    )

    assert [task.task_id for task in tasks] == [
        "task_0000000000000000",
        "task_0000000000000001",
        "task_0000000000000002",
    ]
    assert [[row.position for row in task.rows] for task in tasks] == [
        [0, 1],
        [2, 3],
        [4],
    ]
    assert all(task.cache_attempt_id == ATTEMPT_ID for task in tasks)


def test_batch_task_is_one_whole_node_envelope() -> None:
    task = make_batch_task(
        node_name="Batch_1",
        invocation_id=INVOCATION_ID,
        cache_attempt_id=None,
        tool=ORIGIN,
        rows=_rows(3),
        batch_context={
            "run_dir": "/shared/run",
            "assets_dir": "/shared/run/assets",
            "work_dir": "/shared/run/work",
            "rows_dir": "/shared/run/work/rows",
            "row_dir": None,
            "batch_dir": "/shared/run/work/batch",
            "row_index": None,
        },
    )

    assert task.mode == "process_batch"
    assert task.task_id == "task_0000000000000000"
    assert [row.position for row in task.rows] == [0, 1, 2]


def test_collector_is_bounded_and_emits_row_completion_in_position_order() -> None:
    tasks = list(
        iter_row_tasks(
            node_name="Tool_1",
            invocation_id=INVOCATION_ID,
            cache_attempt_id=ATTEMPT_ID,
            tool=ORIGIN,
            rows=_rows(6),
            row_chunk_size=1,
        )
    )
    active: set[concurrent.futures.Future] = set()
    peak = 0
    events: list[int] = []
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=6)

    def submit(task):
        return pool.submit(_result, task, value_offset=10)

    def register(future, _task):
        nonlocal peak
        active.add(future)
        peak = max(peak, len(active))

    def release(future, _task):
        active.remove(future)

    try:
        rows = BoundedParslCollector(
            submit=submit,
            max_in_flight=2,
            node_ordinal=3,
            executor_label="cpu",
            cancel_requested=lambda: False,
            register_future=register,
            release_future=release,
            row_complete=lambda position, _index: events.append(position),
        ).run(tasks)
    finally:
        pool.shutdown()

    assert peak <= 2
    assert [row.position for row in rows] == list(range(6))
    assert events == list(range(6))
    assert active == set()


def test_collector_stops_submission_cancels_and_drains_after_failure() -> None:
    tasks = list(
        iter_row_tasks(
            node_name="Tool_1",
            invocation_id=INVOCATION_ID,
            cache_attempt_id=ATTEMPT_ID,
            tool=ORIGIN,
            rows=_rows(8),
            row_chunk_size=1,
        )
    )
    submitted: list[str] = []
    active: set[concurrent.futures.Future] = set()

    def submit(task):
        submitted.append(task.task_id)
        future: concurrent.futures.Future = concurrent.futures.Future()
        if task.rows[0].position == 0:
            future.set_exception(RuntimeError("failed first"))
        return future

    def register(future, _task):
        active.add(future)

    def release(future, _task):
        active.remove(future)

    with pytest.raises(ParslTaskError, match="failed first") as caught:
        BoundedParslCollector(
            submit=submit,
            max_in_flight=3,
            node_ordinal=7,
            executor_label="cpu",
            cancel_requested=lambda: False,
            register_future=register,
            release_future=release,
        ).run(tasks)

    assert len(submitted) == 3
    assert active == set()
    assert caught.value.failure_order_key == (
        7,
        0,
        "task_0000000000000000",
    )


def test_collector_validates_result_correlation_before_acceptance() -> None:
    [task] = list(
        iter_row_tasks(
            node_name="Tool_1",
            invocation_id=INVOCATION_ID,
            cache_attempt_id=ATTEMPT_ID,
            tool=ORIGIN,
            rows=_rows(1),
            row_chunk_size=1,
        )
    )

    def submit(invocation):
        future: concurrent.futures.Future = concurrent.futures.Future()
        payload = _result(invocation)
        payload["node_name"] = "Other_1"
        future.set_result(payload)
        return future

    with pytest.raises(ParslTaskError, match="correlation"):
        BoundedParslCollector(
            submit=submit,
            max_in_flight=1,
            node_ordinal=0,
            executor_label="cpu",
            cancel_requested=lambda: False,
        ).run([task])


def test_task_iterator_failure_has_deterministic_node_order() -> None:
    observed: list[BaseException] = []

    def broken_tasks() -> Iterator[ProcessingTaskV1]:
        raise RuntimeError("task packing failed")
        yield

    with pytest.raises(RuntimeError, match="task packing failed") as caught:
        BoundedParslCollector(
            submit=lambda _task: pytest.fail("task was submitted"),
            max_in_flight=1,
            node_ordinal=5,
            executor_label="cpu",
            cancel_requested=lambda: False,
            failure_observed=observed.append,
        ).run(broken_tasks())

    assert observed == [caught.value]
    assert caught.value.failure_order_key == (5, -1, "")


def test_cancellation_stops_before_submission() -> None:
    tasks = iter_row_tasks(
        node_name="Tool_1",
        invocation_id=INVOCATION_ID,
        cache_attempt_id=None,
        tool=ORIGIN,
        rows=_rows(2),
        row_chunk_size=1,
    )

    with pytest.raises(WorkflowCancelledError):
        BoundedParslCollector(
            submit=lambda _task: pytest.fail("task was submitted"),
            max_in_flight=1,
            node_ordinal=0,
            executor_label="cpu",
            cancel_requested=lambda: True,
        ).run(tasks)


def test_runtime_shared_array_is_rejected_before_submission() -> None:
    [task] = list(
        iter_row_tasks(
            node_name="Tool_1",
            invocation_id=INVOCATION_ID,
            cache_attempt_id=None,
            tool=ORIGIN,
            rows=[
                RowInvocationV1(
                    position=0,
                    row_index="0",
                    arguments={
                        "image": SharedArray(
                            name="shared",
                            shape=(1,),
                            dtype="uint8",
                        )
                    },
                    context=None,
                )
            ],
            row_chunk_size=1,
        )
    )

    with pytest.raises(TypeError, match="SharedArray"):
        validate_task_runtime_values(task)


def test_late_runtime_validation_failure_cancels_and_drains_prior_future() -> None:
    tasks = list(
        iter_row_tasks(
            node_name="Tool_1",
            invocation_id=INVOCATION_ID,
            cache_attempt_id=None,
            tool=ORIGIN,
            rows=[
                RowInvocationV1(
                    position=0,
                    row_index="0",
                    arguments={"image": "plain"},
                    context=None,
                ),
                RowInvocationV1(
                    position=1,
                    row_index="1",
                    arguments={
                        "image": SharedArray(
                            name="shared",
                            shape=(1,),
                            dtype="uint8",
                        )
                    },
                    context=None,
                ),
            ],
            row_chunk_size=1,
        )
    )
    future: concurrent.futures.Future = concurrent.futures.Future()
    released: list[str] = []
    submitted: list[str] = []

    def submit(task):
        submitted.append(task.task_id)
        return future

    with pytest.raises(TypeError, match="SharedArray"):
        BoundedParslCollector(
            submit=submit,
            max_in_flight=2,
            node_ordinal=0,
            executor_label="cpu",
            cancel_requested=lambda: False,
            release_future=lambda _future, task: released.append(task.task_id),
        ).run(tasks)

    assert submitted == ["task_0000000000000000"]
    assert future.cancelled()
    assert released == ["task_0000000000000000"]


def test_cancellation_stops_partial_window_and_drains_running_futures() -> None:
    tasks = list(
        iter_row_tasks(
            node_name="Tool_1",
            invocation_id=INVOCATION_ID,
            cache_attempt_id=ATTEMPT_ID,
            tool=ORIGIN,
            rows=_rows(5),
            row_chunk_size=1,
        )
    )
    submitted: list[tuple[object, concurrent.futures.Future]] = []
    window_ready = Event()
    cancelled = Event()
    released: list[str] = []
    failures: list[BaseException] = []

    def submit(task):
        future: concurrent.futures.Future = concurrent.futures.Future()
        assert future.set_running_or_notify_cancel()
        submitted.append((task, future))
        if len(submitted) == 2:
            window_ready.set()
        return future

    def collect() -> None:
        try:
            BoundedParslCollector(
                submit=submit,
                max_in_flight=2,
                node_ordinal=0,
                executor_label="cpu",
                cancel_requested=cancelled.is_set,
                release_future=lambda _future, task: released.append(task.task_id),
            ).run(tasks)
        except BaseException as exc:
            failures.append(exc)

    thread = Thread(target=collect)
    thread.start()
    assert window_ready.wait(timeout=5)
    cancelled.set()
    for task, future in submitted:
        future.set_result(_result(task))
    thread.join(timeout=5)

    assert not thread.is_alive()
    assert len(submitted) == 2
    assert len(released) == 2
    assert len(failures) == 1
    assert isinstance(failures[0], WorkflowCancelledError)


@pytest.mark.parametrize("first_failure", ["high", "low"])
def test_sibling_collectors_report_late_failures_for_deterministic_selection(
    first_failure: str,
) -> None:
    low_task = next(
        iter_row_tasks(
            node_name="Low_1",
            invocation_id=INVOCATION_ID,
            cache_attempt_id=ATTEMPT_ID,
            tool=ORIGIN,
            rows=_rows(1),
            row_chunk_size=1,
        )
    )
    high_task = next(
        iter_row_tasks(
            node_name="High_1",
            invocation_id=INVOCATION_ID,
            cache_attempt_id=ATTEMPT_ID,
            tool=ORIGIN,
            rows=_rows(1),
            row_chunk_size=1,
        )
    )
    low_future: concurrent.futures.Future = concurrent.futures.Future()
    high_future: concurrent.futures.Future = concurrent.futures.Future()
    assert low_future.set_running_or_notify_cancel()
    assert high_future.set_running_or_notify_cancel()
    stop = Event()
    observed: list[ParslTaskError] = []
    failures: list[BaseException] = []

    def report(failure: ParslTaskError) -> None:
        observed.append(failure)
        stop.set()

    def collect(task, future, ordinal: int) -> None:
        try:
            BoundedParslCollector(
                submit=lambda _task: future,
                max_in_flight=1,
                node_ordinal=ordinal,
                executor_label="cpu",
                cancel_requested=lambda: False,
                stop_requested=stop.is_set,
                failure_observed=report,
                stopped_error=lambda: min(
                    observed,
                    key=lambda failure: failure.failure_order_key,
                ),
            ).run([task])
        except BaseException as exc:
            failures.append(exc)

    low_thread = Thread(target=collect, args=(low_task, low_future, 0))
    high_thread = Thread(target=collect, args=(high_task, high_future, 1))
    low_thread.start()
    high_thread.start()
    first_future = high_future if first_failure == "high" else low_future
    second_future = low_future if first_failure == "high" else high_future
    first_future.set_exception(RuntimeError(f"{first_failure} failed first"))
    assert stop.wait(timeout=5)
    second_future.set_exception(RuntimeError("sibling failed later"))
    low_thread.join(timeout=5)
    high_thread.join(timeout=5)

    assert not low_thread.is_alive()
    assert not high_thread.is_alive()
    assert sorted(
        failure.failure_order_key for failure in observed
    ) == [
        (0, 0, "task_0000000000000000"),
        (1, 0, "task_0000000000000000"),
    ]
    primary = min(
        (error for error in failures if isinstance(error, ParslTaskError)),
        key=lambda failure: failure.failure_order_key,
    )
    assert primary.scoped_node_name == "Low_1"
