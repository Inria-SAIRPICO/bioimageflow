"""Pre-submission failure ordering."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from bioimageflow.parsl.submission import BoundedParslCollector
from bioimageflow_core import ProcessingTaskV1


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
