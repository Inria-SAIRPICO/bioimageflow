"""Startup ordering and pre-DFK rejection tests."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from bioimageflow import (
    ExecutorBinding,
    ExecutorCapabilities,
    ParslEngine,
    WorkerEnvironmentAttestation,
    WorkerSlotCapacity,
    Workflow,
)
from bioimageflow.cache import compute_env_hash
from bioimageflow.parsl.startup import CORE_REQUIREMENT
from bioimageflow_core import (
    Arguments,
    ImageShared,
    IOModel,
    ProcessingTool,
)
from tests.testkit.parsl_tools import PARSL_TEST_ENV, ParslIncrement


def _binding(label: str) -> ExecutorBinding:
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
            tool_origin_modes=("shared_module", "source_file"),
            slot=WorkerSlotCapacity(cpu=1),
        ),
    )


class _NoStartEngine(ParslEngine):
    def _start_attached_execution(self):
        raise AssertionError("DFK acquisition must not occur")


def _workflow(tmp_path, tool: ProcessingTool) -> tuple[Workflow, object]:
    workflow = Workflow(storage_path=tmp_path, engine="direct")
    with workflow:
        node = tool(value=3)
    return workflow, node


def test_nonzero_config_retries_fail_before_dfk_acquisition(tmp_path) -> None:
    workflow, node = _workflow(tmp_path, ParslIncrement())
    engine = _NoStartEngine(
        parsl_config=SimpleNamespace(
            retries=1,
            executors=(SimpleNamespace(label="cpu"),),
        ),
        executor_bindings={"cpu": _binding("cpu")},
    )

    with pytest.raises(ValueError, match="retries=0"):
        workflow.compute(node, engine=engine)


def test_ambiguous_compatible_routes_fail_before_dfk_acquisition(
    tmp_path,
) -> None:
    workflow, node = _workflow(tmp_path, ParslIncrement())
    engine = _NoStartEngine(
        parsl_config=SimpleNamespace(
            retries=0,
            executors=(
                SimpleNamespace(label="first"),
                SimpleNamespace(label="second"),
            ),
        ),
        executor_bindings={
            "first": _binding("first"),
            "second": _binding("second"),
        },
    )

    with pytest.raises(ValueError, match="ambiguous"):
        workflow.compute(node, engine=engine)


class _SharedOutput(ProcessingTool):
    environment = PARSL_TEST_ENV

    class Inputs(IOModel):
        value: int = 1

    class Outputs(IOModel):
        image: ImageShared()

    def process_row(self, arguments: Arguments):
        del arguments
        raise AssertionError("processing must not run")


def test_remote_shared_memory_schema_fails_before_dfk_acquisition(
    tmp_path,
) -> None:
    workflow, node = _workflow(tmp_path, _SharedOutput())
    engine = _NoStartEngine(
        parsl_config=SimpleNamespace(
            retries=0,
            executors=(SimpleNamespace(label="cpu"),),
        ),
        executor_bindings={"cpu": _binding("cpu")},
    )

    with pytest.raises(TypeError, match="SharedArray"):
        workflow.compute(node, engine=engine)
