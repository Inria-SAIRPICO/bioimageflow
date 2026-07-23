"""Bounded Parsl task packing, submission, draining, and collection."""

from __future__ import annotations

import concurrent.futures
import time
import traceback
from collections.abc import Callable, Iterable, Iterator
from typing import Any, Protocol

from bioimageflow.engine import WorkflowCancelledError
from bioimageflow.parsl.errors import ParslTaskError
from bioimageflow_core import (
    ProcessingTaskResultV1,
    ProcessingTaskV1,
    RowInvocationV1,
    RowResultV1,
    decode_processing_result,
    validate_processing_result,
)
from bioimageflow_core.types import SharedArray
from bioimageflow_core.worker_origins import WorkerToolOriginV1


class ParslFuture(Protocol):
    """Future operations required from a Parsl AppFuture."""

    def cancel(self) -> bool: ...

    def done(self) -> bool: ...

    def result(self) -> Any: ...


def effective_in_flight_limit(
    max_in_flight: int,
    max_concurrent: int,
    *,
    sequential: bool,
) -> int:
    """Return the exact per-node unfinished-future bound."""
    for field, value, minimum in (
        ("max_in_flight", max_in_flight, 1),
        ("max_concurrent", max_concurrent, 0),
    ):
        if type(value) is not int or value < minimum:
            raise ValueError(f"{field} must be an integer >= {minimum}.")
    if sequential:
        return 1
    if max_concurrent > 0:
        return min(max_in_flight, max_concurrent)
    return max_in_flight


def iter_row_tasks(
    *,
    node_name: str,
    invocation_id: str,
    cache_attempt_id: str | None,
    tool: WorkerToolOriginV1,
    rows: Iterable[RowInvocationV1],
    row_chunk_size: int,
) -> Iterator[ProcessingTaskV1]:
    """Yield consecutive row chunks without constructing an unbounded future list."""
    if type(row_chunk_size) is not int or row_chunk_size < 1:
        raise ValueError("row_chunk_size must be an integer >= 1.")
    chunk: list[RowInvocationV1] = []
    sequence = 0
    for row in rows:
        chunk.append(row)
        if len(chunk) < row_chunk_size:
            continue
        yield ProcessingTaskV1(
            task_id=f"task_{sequence:016x}",
            node_name=node_name,
            invocation_id=invocation_id,
            cache_attempt_id=cache_attempt_id,
            task_retry=0,
            mode="row_chunk",
            tool=tool,
            rows=tuple(chunk),
        )
        sequence += 1
        chunk = []
    if chunk:
        yield ProcessingTaskV1(
            task_id=f"task_{sequence:016x}",
            node_name=node_name,
            invocation_id=invocation_id,
            cache_attempt_id=cache_attempt_id,
            task_retry=0,
            mode="row_chunk",
            tool=tool,
            rows=tuple(chunk),
        )


def make_batch_task(
    *,
    node_name: str,
    invocation_id: str,
    cache_attempt_id: str | None,
    tool: WorkerToolOriginV1,
    rows: Iterable[RowInvocationV1],
    batch_context: dict[str, Any],
) -> ProcessingTaskV1:
    """Build the one whole-node process_batch envelope."""
    return ProcessingTaskV1(
        task_id="task_0000000000000000",
        node_name=node_name,
        invocation_id=invocation_id,
        cache_attempt_id=cache_attempt_id,
        task_retry=0,
        mode="process_batch",
        tool=tool,
        rows=tuple(rows),
        batch_context=batch_context,
    )


def _contains_shared_array(value: Any) -> bool:
    if isinstance(value, SharedArray):
        return True
    if isinstance(value, dict):
        return any(
            _contains_shared_array(key) or _contains_shared_array(item)
            for key, item in value.items()
        )
    if isinstance(value, (list, tuple)):
        return any(_contains_shared_array(item) for item in value)
    return False


def validate_task_runtime_values(task: ProcessingTaskV1) -> None:
    """Reject SharedArray values immediately before remote submission."""
    for row in task.rows:
        if _contains_shared_array(row.arguments):
            raise TypeError(
                f"Task {task.task_id} contains a SharedArray argument that cannot "
                "cross the Parsl boundary."
            )


class BoundedParslCollector:
    """Run one node's task stream with bounded futures and deterministic drain."""

    def __init__(
        self,
        *,
        submit: Callable[[ProcessingTaskV1], ParslFuture],
        max_in_flight: int,
        node_ordinal: int,
        executor_label: str,
        cancel_requested: Callable[[], bool],
        register_future: Callable[
            [ParslFuture, ProcessingTaskV1], None
        ] = lambda _future, _task: None,
        release_future: Callable[
            [ParslFuture, ProcessingTaskV1], None
        ] = lambda _future, _task: None,
        row_complete: Callable[[int, str], None] = lambda _position, _index: None,
    ) -> None:
        if type(max_in_flight) is not int or max_in_flight < 1:
            raise ValueError("max_in_flight must be an integer >= 1.")
        self._submit = submit
        self._max_in_flight = max_in_flight
        self._node_ordinal = node_ordinal
        self._executor_label = executor_label
        self._cancel_requested = cancel_requested
        self._register_future = register_future
        self._release_future = release_future
        self._row_complete = row_complete

    def run(self, tasks: Iterable[ProcessingTaskV1]) -> tuple[RowResultV1, ...]:
        """Submit lazily, accept by position, and drain every submitted future."""
        task_iterator = iter(tasks)
        active: dict[ParslFuture, ProcessingTaskV1] = {}
        accepted: dict[int, RowResultV1] = {}
        failures: list[ParslTaskError] = []
        stopped = False
        cancelled = False
        pre_submission_error: BaseException | None = None
        next_row_complete = 0

        def stop_and_cancel() -> None:
            nonlocal stopped
            stopped = True
            for future in tuple(active):
                if not future.done():
                    future.cancel()

        def submit_next() -> bool:
            nonlocal cancelled, pre_submission_error
            try:
                task = next(task_iterator)
            except StopIteration:
                return False
            except BaseException as exc:
                pre_submission_error = exc
                stop_and_cancel()
                return False
            if self._cancel_requested():
                cancelled = True
                stop_and_cancel()
                return False
            try:
                validate_task_runtime_values(task)
                future = self._submit(task)
                active[future] = task
                self._register_future(future, task)
            except BaseException as exc:
                pre_submission_error = exc
                stop_and_cancel()
                return False
            return True

        while not stopped and len(active) < self._max_in_flight:
            if not submit_next():
                break

        while active:
            if self._cancel_requested() and not cancelled:
                cancelled = True
                stop_and_cancel()
            completed = {future for future in active if future.done()}
            if not completed:
                time.sleep(0.01)
                completed = {future for future in active if future.done()}
            if not completed:
                continue
            for future in completed:
                task = active[future]
                try:
                    raw_result = future.result()
                    if cancelled or failures:
                        continue
                    result = (
                        raw_result
                        if isinstance(raw_result, ProcessingTaskResultV1)
                        else decode_processing_result(raw_result)
                    )
                    validate_processing_result(task, result)
                    for row in result.rows:
                        if row.position in accepted:
                            raise ValueError(
                                f"Duplicate accepted row position {row.position}."
                            )
                        accepted[row.position] = row
                    if task.mode == "row_chunk":
                        while next_row_complete in accepted:
                            row = accepted[next_row_complete]
                            self._row_complete(row.position, row.row_index)
                            next_row_complete += 1
                except concurrent.futures.CancelledError:
                    if (
                        not cancelled
                        and not failures
                        and pre_submission_error is None
                    ):
                        cancelled = True
                        stop_and_cancel()
                except BaseException as exc:
                    failure = self._task_error(task, exc)
                    failures.append(failure)
                    stop_and_cancel()
                finally:
                    self._release_future(future, task)
                    del active[future]

            while not stopped and len(active) < self._max_in_flight:
                if not submit_next():
                    break

        if failures:
            raise min(
                failures,
                key=lambda failure: failure.failure_order_key,
            )
        if pre_submission_error is not None:
            raise pre_submission_error
        if cancelled or self._cancel_requested():
            raise WorkflowCancelledError("Workflow cancelled during Parsl execution.")
        return tuple(accepted[position] for position in sorted(accepted))

    def _task_error(
        self,
        task: ProcessingTaskV1,
        error: BaseException,
    ) -> ParslTaskError:
        positions = tuple(row.position for row in task.rows)
        if task.mode == "process_batch":
            row_position: int | tuple[int, int] | None = None
            first_position = -1
        elif len(positions) == 1:
            row_position = positions[0]
            first_position = positions[0]
        else:
            row_position = (positions[0], positions[-1])
            first_position = positions[0]
        remote_traceback = "".join(
            traceback.format_exception(type(error), error, error.__traceback__)
        )
        failure = ParslTaskError(
            scoped_node_name=task.node_name,
            tool_origin=task.tool,
            executor_label=self._executor_label,
            task_id=task.task_id,
            invocation_id=task.invocation_id,
            cache_attempt_id=task.cache_attempt_id,
            task_retry=task.task_retry,
            row_position=row_position,
            original_type=type(error).__name__,
            original_message=str(error),
            remote_traceback=remote_traceback,
        )
        failure.failure_order_key = (
            self._node_ordinal,
            first_position,
            task.task_id,
        )
        return failure


__all__ = [
    "BoundedParslCollector",
    "ParslFuture",
    "effective_in_flight_limit",
    "iter_row_tasks",
    "make_batch_task",
    "validate_task_runtime_values",
]
