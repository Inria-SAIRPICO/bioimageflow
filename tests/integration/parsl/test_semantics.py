"""Real ThreadPool acceptance semantics for attached Parsl execution."""

from __future__ import annotations

import json
import threading
from pathlib import Path

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
    Workflow,
    WorkflowExecutionContext,
)
from bioimageflow.cache import compute_env_hash
from bioimageflow.parsl.startup import CORE_REQUIREMENT
from bioimageflow_core import Arguments, IOModel
from tests.integration.runtime_cache.test_recursive_provenance import (
    ValueAssetWriter,
    _build_non_reusable_workflow,
)
from tests.integration.parsl.test_thread_executor import (
    _Rows,
    _binding,
    _thread_config,
)
from tests.testkit.parsl_tools import (
    ParslConcurrencyProbe,
    ParslDirectoryWriter,
    ParslDrop,
    ParslExplode,
    ParslFlatBatch,
    ParslIncrement,
    ParslRuntimeSharedArray,
)


class _ThreadIdentityRows(DataFrameTool):
    thread_id: int | None = None

    class Inputs(IOModel):
        value: int

    class Outputs(IOModel):
        value: int

    def transform(self, df: pd.DataFrame, arguments: Arguments) -> pd.DataFrame:
        type(self).thread_id = threading.get_ident()
        return pd.DataFrame({"value": [arguments.value]}, index=["row"])


@pytest.mark.parsl
def test_real_parsl_explosion_empty_rows_and_flat_batch(tmp_path: Path) -> None:
    workflow = Workflow(storage_path=tmp_path, engine="direct")
    with workflow:
        rows = _Rows()(values=(1, 2))
        exploded = ParslExplode()(value=rows["value"], name="exploded")
        dropped = ParslDrop()(value=rows["value"], name="dropped")
        flat = ParslFlatBatch()(value=rows["value"], name="flat")
    engine = ParslEngine(
        parsl_config=_thread_config(tmp_path, max_threads=4),
        executor_bindings={"threads": _binding()},
    )

    results = workflow.compute(exploded, dropped, flat, engine=engine)

    assert list(results["exploded"].index) == [
        "row-0::0",
        "row-0::1",
        "row-1::0",
        "row-1::1",
    ]
    assert list(results["exploded"]["value"]) == [1, 2, 2, 3]
    assert results["dropped"].empty
    assert list(results["dropped"].columns) == ["value"]
    assert list(results["flat"]["value"]) == [2, 3]


@pytest.mark.parsl
def test_real_parsl_directory_asset_cache_and_output_view(
    tmp_path: Path,
) -> None:
    events = []
    workflow = Workflow(
        storage_path=tmp_path,
        engine="direct",
        output_view="copy",
        on_progress=events.append,
    )
    with workflow:
        writer = ParslDirectoryWriter()(value=12, name="writer")
    engine = ParslEngine(
        parsl_config=_thread_config(tmp_path),
        executor_bindings={"threads": _binding()},
    )
    context = WorkflowExecutionContext()

    first = workflow.compute(writer, engine=engine, run_context=context)
    exported = context.export_result(first, destination=tmp_path / "exported")
    second = workflow.compute(writer, engine=engine)

    first_path = Path(first.loc["0", "directory"])
    second_path = Path(second.loc["0", "directory"])
    assert first_path == second_path
    assert first_path.name == "dataset_0.zarr"
    assert first_path.joinpath("value.txt").read_text() == "12"
    exported_path = Path(exported.loc["0", "directory"])
    assert exported_path.is_relative_to(tmp_path / "exported")
    assert exported_path.joinpath("value.txt").read_text() == "12"
    assert (
        tmp_path
        / "outputs"
        / "latest"
        / "writer"
        / "dataset_0.zarr"
        / "value.txt"
    ).read_text() == "12"
    assert any(event.node_name == "writer" and event.status == "cached" for event in events)


@pytest.mark.parsl
def test_runtime_shared_array_output_fails_before_publication(
    tmp_path: Path,
) -> None:
    workflow = Workflow(storage_path=tmp_path, engine="direct")
    with workflow:
        node = ParslRuntimeSharedArray()(value=1)
    engine = ParslEngine(
        parsl_config=_thread_config(tmp_path),
        executor_bindings={"threads": _binding()},
    )

    with pytest.raises(TypeError, match="SharedArray"):
        workflow.compute(node, engine=engine)

    assert workflow.plan()[node.name].selected_record_id is None


@pytest.mark.parsl
def test_parallel_and_sequential_policies_bound_worker_overlap(
    tmp_path: Path,
) -> None:
    def build(storage_path: Path) -> tuple[Workflow, object]:
        workflow = Workflow(storage_path=storage_path, engine="direct")
        with workflow:
            rows = _Rows()(values=(1, 2, 3, 4))
            probe = ParslConcurrencyProbe()(value=rows["value"])
        return workflow, probe

    parallel, parallel_probe = build(tmp_path / "parallel")
    parallel_result = parallel.compute(
        parallel_probe,
        engine=ParslEngine(
            parsl_config=_thread_config(tmp_path / "parallel", max_threads=4),
            executor_bindings={"threads": _binding()},
            task_policy=ParslTaskPolicy(max_in_flight=4),
            execution="parallel",
        ),
    )
    assert (
        max(parallel_result["started_ns"])
        < min(parallel_result["finished_ns"])
    )

    sequential, sequential_probe = build(tmp_path / "sequential")
    sequential_result = sequential.compute(
        sequential_probe,
        engine=ParslEngine(
            parsl_config=_thread_config(tmp_path / "sequential", max_threads=4),
            executor_bindings={"threads": _binding()},
            task_policy=ParslTaskPolicy(max_in_flight=4),
            execution="sequential",
        ),
    )
    intervals = sorted(
        zip(
            sequential_result["started_ns"],
            sequential_result["finished_ns"],
        )
    )
    assert all(
        next_started >= finished
        for (_started, finished), (next_started, _next_finished) in zip(
            intervals,
            intervals[1:],
        )
    )


@pytest.mark.parsl
def test_two_executor_labels_route_and_preflight_in_one_run(
    tmp_path: Path,
) -> None:
    workflow = Workflow(storage_path=tmp_path, engine="direct")
    with workflow:
        first = ParslIncrement()(value=1, name="first")
        second = ParslIncrement()(value=2, name="second")
    config = Config(
        executors=[
            ThreadPoolExecutor(label="first-label", max_threads=1),
            ThreadPoolExecutor(label="second-label", max_threads=1),
        ],
        retries=0,
        run_dir=str(tmp_path / "runinfo"),
        usage_tracking=0,
    )
    engine = ParslEngine(
        parsl_config=config,
        executor_bindings={
            "first-label": _binding("first-label"),
            "second-label": _binding("second-label"),
        },
        node_routes={
            "first": "first-label",
            "second": "second-label",
        },
    )

    results = workflow.compute(first, second, engine=engine)

    assert results["first"].loc["0", "value"] == 2
    assert results["second"].loc["0", "value"] == 3
    diagnostics = [
        json.loads(path.read_text())
        for path in (tmp_path / "diagnostics" / "v1" / "runs").rglob(
            "task_*.json"
        )
    ]
    assert {item["executor_label"] for item in diagnostics} == {
        "first-label",
        "second-label",
    }


@pytest.mark.parsl
def test_dataframe_tool_runs_on_orchestrator_thread(tmp_path: Path) -> None:
    import importlib

    increment_type = importlib.import_module(
        "tests.testkit.parsl_tools"
    ).ParslIncrement
    workflow = Workflow(storage_path=tmp_path, engine="direct")
    with workflow:
        rows = _ThreadIdentityRows()(value=3)
        processed = increment_type()(value=rows["value"])
    engine = ParslEngine(
        parsl_config=_thread_config(tmp_path),
        executor_bindings={"threads": _binding()},
    )
    orchestrator_thread = threading.get_ident()

    result = workflow.compute(processed, engine=engine)

    assert result.loc["row", "value"] == 4
    assert _ThreadIdentityRows.thread_id == orchestrator_thread


@pytest.mark.parsl
def test_non_reusable_recursive_path_uses_parsl_transient_workspace(
    tmp_path: Path,
) -> None:
    workflow, _consumer, writer = _build_non_reusable_workflow(tmp_path)
    base = _binding()
    writer_environment = ValueAssetWriter.environment
    binding = ExecutorBinding(
        label="threads",
        environments=base.environments
        + (
            WorkerEnvironmentAttestation(
                name=writer_environment.name,
                dependency_hash=compute_env_hash(
                    writer_environment.dependencies
                ),
                allow_flexible_versions=False,
                core_requirement=CORE_REQUIREMENT,
            ),
        ),
        capabilities=ExecutorCapabilities(
            storage_modes=base.capabilities.storage_modes,
            tool_origin_modes=base.capabilities.tool_origin_modes,
            slot=base.capabilities.slot,
        ),
    )
    engine = ParslEngine(
        parsl_config=_thread_config(tmp_path),
        executor_bindings={"threads": binding},
    )

    result = workflow.compute(engine=engine)

    assert result.loc["row", "copied"] == 14
    [invocation_path] = list(
        (
            tmp_path
            / "cache"
            / "v1"
            / "transient"
            / "runs"
        ).glob("run_*/nodes/writer/inv_*/invocation.json")
    )
    invocation = json.loads(invocation_path.read_text())
    assert invocation["engine"] == "parsl:parallel"
    assert invocation["status"] == "succeeded"
    [diagnostic_path] = list(
        (tmp_path / "diagnostics" / "v1" / "runs").rglob("task_*.json")
    )
    diagnostic = json.loads(diagnostic_path.read_text())
    assert diagnostic["node_key"] == writer.name
    assert diagnostic["cache_attempt_id"] is None
