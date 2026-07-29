"""Process-isolated local Parsl release-gate coverage."""

from __future__ import annotations

import os

import pandas as pd
import pytest
from parsl import Config
from parsl.executors import HighThroughputExecutor
from parsl.providers import LocalProvider

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
from bioimageflow.parsl.startup import CORE_REQUIREMENT
from bioimageflow_core import Arguments, IOModel
from tests.testkit.parsl_tools import (
    PARSL_TEST_ENV,
    ParslProcessIdentity,
)


class _Rows(DataFrameTool):
    class Inputs(IOModel):
        values: tuple[int, ...]

    class Outputs(IOModel):
        value: int

    def transform(self, df: pd.DataFrame, arguments: Arguments) -> pd.DataFrame:
        return pd.DataFrame(
            {"value": list(arguments.values)},
            index=["first", "second"],
        )


def _binding() -> ExecutorBinding:
    return ExecutorBinding(
        label="processes",
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


@pytest.mark.parsl
@pytest.mark.slow
def test_local_htex_process_serialization_imports_and_shared_paths(
    tmp_path,
) -> None:
    workflow = Workflow(storage_path=tmp_path / "storage", engine="direct")
    with workflow:
        rows = _Rows()(values=(4, 8))
        identities = ParslProcessIdentity()(value=rows["value"])
    config = Config(
        executors=[
            HighThroughputExecutor(
                label="processes",
                provider=LocalProvider(
                    init_blocks=1,
                    min_blocks=0,
                    max_blocks=1,
                ),
                max_workers_per_node=1,
                working_dir=str(tmp_path / "workers"),
                worker_logdir_root=str(tmp_path / "worker-logs"),
                encrypted=False,
            )
        ],
        retries=0,
        run_dir=str(tmp_path / "runinfo"),
        usage_tracking=0,
    )
    engine = ParslEngine(
        parsl_config=config,
        executor_bindings={"processes": _binding()},
        task_policy=ParslTaskPolicy(
            row_chunk_size=1,
            max_in_flight=1,
        ),
    )

    result = workflow.compute(identities, engine=engine)

    assert list(result["value"]) == [4, 8]
    assert all(process_id != os.getpid() for process_id in result["process_id"])
    assert len(set(result["process_id"])) == 1
    assert len(set(result["instance_id"])) == 1
