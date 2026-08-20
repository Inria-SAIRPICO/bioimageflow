"""Deterministic workflow-wide failure selection."""

from __future__ import annotations

import time

import pytest

from bioimageflow import Workflow
from bioimageflow_core import Arguments, EnvironmentSpec, IOModel, ProcessingTool, RowConsumption


class _DelayedFailure(ProcessingTool):
    row_consumption = RowConsumption.MAPPED
    environment = EnvironmentSpec(name="delayed_failure", dependencies={})

    class Inputs(IOModel):
        delay: float
        message: str

    class Outputs(IOModel):
        value: int

    def process_row(self, arguments: Arguments):
        time.sleep(arguments.delay)
        raise RuntimeError(arguments.message)


def test_parallel_failures_use_compiled_order_not_completion_order(tmp_path) -> None:
    with Workflow(engine="direct", storage_path=tmp_path) as workflow:
        slow_first = _DelayedFailure()(
            delay=0.15,
            message="deterministic-primary",
            name="a_slow",
        )
        fast_second = _DelayedFailure()(
            delay=0.01,
            message="completion-race-loser",
            name="z_fast",
        )

    with pytest.raises(RuntimeError, match="deterministic-primary"):
        workflow.compute(fast_second, slow_first)
