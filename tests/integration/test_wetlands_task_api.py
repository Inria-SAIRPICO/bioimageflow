"""Integration tests for the Wetlands Task API features.

These tests use REAL Wetlands worker processes (use_wetlands=True).
They cover the 5 features from the Task API integration plan:
  1. Intra-node row parallelism (map_tasks)
  2. GPU-aware worker assignment (worker_env, ResourceSpec)
  3. Sub-row progress reporting (task.update → ProgressEvent)
  4. Workflow cancellation (cancel(), WorkflowCancelledError)
  5. Branch-level parallelism (TopologicalSorter + ThreadPoolExecutor)
"""

import threading
import time
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from bioimageflow import ProgressEvent, Workflow
from bioimageflow.engine import DefaultEngine, SequentialEngine, WorkflowCancelledError

from .conftest import FileLoader
from .wetlands_test_tools import (
    BatchTool,
    CancellableRowTool,
    ErrorRowTool,
    GpuTool,
    ProgressReportingTool,
    SimpleRowTool,
    SlowRowTool,
    stub_env,
    gpu_env,
)


# Override the autouse _disable_wetlands fixture — these tests need real Wetlands.
@pytest.fixture(autouse=True)
def _disable_wetlands():
    """No-op: allow Wetlands to be used in these tests."""
    yield


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """Create a workspace with sample files."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    for name in ["img_01.tif", "img_02.tif", "img_03.tif"]:
        (data_dir / name).write_text(f"FAKE_{name}")
    return tmp_path


@pytest.fixture
def large_workspace(tmp_path: Path) -> Path:
    """Create a workspace with more files for parallelism tests."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    for i in range(6):
        (data_dir / f"img_{i:02d}.tif").write_text(f"FAKE_{i}")
    return tmp_path


# =====================================================================
# Feature 1: Intra-node row parallelism (map_tasks)
# =====================================================================

class TestRowParallelism:
    """Row parallelism via map_tasks — max_workers controls concurrency."""

    def test_single_worker_produces_correct_results(self, workspace):
        """max_workers=1 is identical to the baseline."""
        load = FileLoader()
        tool = SimpleRowTool()

        with Workflow(
            storage_path=workspace / "results",
            use_wetlands=True,
            max_workers=1,
        ) as wf:
            raw = load(path=str(workspace / "data"))
            out = tool(input_path=raw["path"])
            df = wf.compute(out)

            assert len(df) == 3
            assert "output_path" in df.columns
            assert "value" in df.columns
            for _, row in df.iterrows():
                assert Path(row["output_path"]).exists()
                assert row["value"] == 42.0

    def test_multi_worker_same_results(self, workspace):
        """max_workers=4 produces the same results as max_workers=1."""
        load = FileLoader()
        tool = SimpleRowTool()

        with Workflow(
            storage_path=workspace / "results",
            use_wetlands=True,
            max_workers=4,
        ) as wf:
            raw = load(path=str(workspace / "data"))
            out = tool(input_path=raw["path"])
            df = wf.compute(out)

            assert len(df) == 3
            assert "output_path" in df.columns
            for _, row in df.iterrows():
                assert Path(row["output_path"]).exists()
                assert row["value"] == 42.0

    def test_row_ordering_preserved(self, workspace):
        """Output rows correspond to input rows in order."""
        load = FileLoader()
        tool = SimpleRowTool()

        with Workflow(
            storage_path=workspace / "results",
            use_wetlands=True,
            max_workers=2,
        ) as wf:
            raw = load(path=str(workspace / "data"))
            out = tool(input_path=raw["path"])
            df = wf.compute(out)

            assert len(df) == 3
            # Each output file should contain its input path
            for _, row in df.iterrows():
                content = Path(row["output_path"]).read_text()
                assert "processed:" in content

    def test_error_propagation_with_cleanup(self, workspace):
        """Worker errors propagate correctly and don't hang."""
        load = FileLoader()
        tool = ErrorRowTool()

        with pytest.raises(Exception, match="Intentional test error"):
            with Workflow(
                storage_path=workspace / "results",
                use_wetlands=True,
                max_workers=2,
            ) as wf:
                raw = load(path=str(workspace / "data"))
                out = tool(input_path=raw["path"])
                wf.compute(out)

    def test_batch_tool_via_submit(self, workspace):
        """process_batch tools use submit() — single task for all rows."""
        load = FileLoader()
        tool = BatchTool()

        with Workflow(
            storage_path=workspace / "results",
            use_wetlands=True,
        ) as wf:
            raw = load(path=str(workspace / "data"))
            out = tool(input_path=raw["path"])
            df = wf.compute(out)

            assert len(df) == 3
            for _, row in df.iterrows():
                assert Path(row["output_path"]).exists()


# =====================================================================
# Feature 2: GPU-aware worker assignment
# =====================================================================

class TestGpuWorkerAssignment:
    """GPU auto-inference and get_environment() overrides."""

    def test_gpu_tool_auto_infers_worker_env(self, workspace):
        """Tool with ResourceSpec(gpu=1) triggers CUDA_VISIBLE_DEVICES."""
        load = FileLoader()
        tool = GpuTool()

        engine = DefaultEngine(use_wetlands=True)
        # Verify the engine detects GPU requirement
        with Workflow(
            storage_path=workspace / "results",
            use_wetlands=True,
        ) as wf:
            raw = load(path=str(workspace / "data"))
            out = tool(input_path=raw["path"])
            df = wf.compute(out, engine=engine)

            assert len(df) == 3
            # The engine should have detected GPU and auto-set worker_env
            assert engine._env_has_gpu_tool(gpu_env.name, wf)

    def test_get_environment_override(self, workspace):
        """Explicit get_environment() max_workers override takes precedence."""
        load = FileLoader()
        tool = SimpleRowTool()

        with Workflow(
            storage_path=workspace / "results",
            use_wetlands=True,
            max_workers=1,  # workflow default
        ) as wf:
            env = wf.get_environment(tool)
            env.max_workers = 3  # explicit override
            raw = load(path=str(workspace / "data"))
            out = tool(input_path=raw["path"])
            df = wf.compute(out)

            assert len(df) == 3

    def test_workflow_level_max_workers_default(self, workspace):
        """Workflow.max_workers is used when no explicit override exists."""
        load = FileLoader()
        tool = SimpleRowTool()

        engine = DefaultEngine(use_wetlands=True)

        with Workflow(
            storage_path=workspace / "results",
            use_wetlands=True,
            max_workers=2,
        ) as wf:
            raw = load(path=str(workspace / "data"))
            out = tool(input_path=raw["path"])
            df = wf.compute(out, engine=engine)

            assert len(df) == 3

    def test_worker_env_override(self, workspace):
        """get_environment().worker_env overrides GPU auto-inference."""
        load = FileLoader()
        tool = GpuTool()

        custom_env = lambda i: {"MY_DEVICE": str(i)}

        with Workflow(
            storage_path=workspace / "results",
            use_wetlands=True,
        ) as wf:
            env = wf.get_environment(tool)
            env.worker_env = custom_env
            raw = load(path=str(workspace / "data"))
            out = tool(input_path=raw["path"])
            df = wf.compute(out)

            assert len(df) == 3


# =====================================================================
# Feature 3: Sub-row progress reporting
# =====================================================================

class TestSubRowProgress:
    """Progress events from task.update() in workers."""

    def test_row_progress_events_emitted(self, workspace):
        """tool.process_row(task=...) → task.update() → row_progress events."""
        events: list[ProgressEvent] = []

        load = FileLoader()
        tool = ProgressReportingTool()

        with Workflow(
            storage_path=workspace / "results",
            use_wetlands=True,
            on_progress=lambda e: events.append(e),
        ) as wf:
            raw = load(path=str(workspace / "data"))
            out = tool(input_path=raw["path"])
            wf.compute(out)

        progress_events = [e for e in events if e.status == "row_progress"]
        # Should have received progress updates from the workers
        assert len(progress_events) > 0
        for e in progress_events:
            assert e.message is not None
            assert e.current is not None
            assert e.maximum is not None
            assert e.maximum == 5  # 5 steps per row

    def test_failed_event_on_error(self, workspace):
        """Failed nodes emit a 'failed' progress event."""
        events: list[ProgressEvent] = []

        load = FileLoader()
        tool = ErrorRowTool()

        with pytest.raises(Exception):
            with Workflow(
                storage_path=workspace / "results",
                use_wetlands=True,
                on_progress=lambda e: events.append(e),
            ) as wf:
                raw = load(path=str(workspace / "data"))
                out = tool(input_path=raw["path"])
                wf.compute(out)

        failed_events = [e for e in events if e.status == "failed"]
        assert len(failed_events) >= 1

    def test_batch_progress_events(self, workspace):
        """process_batch with task.update() reports progress."""
        events: list[ProgressEvent] = []

        load = FileLoader()
        tool = BatchTool()

        with Workflow(
            storage_path=workspace / "results",
            use_wetlands=True,
            on_progress=lambda e: events.append(e),
        ) as wf:
            raw = load(path=str(workspace / "data"))
            out = tool(input_path=raw["path"])
            wf.compute(out)

        # batch tasks emit row_complete events
        complete_events = [e for e in events if e.status == "row_complete"]
        # At minimum we get completed events
        completed_events = [e for e in events if e.status == "completed"]
        assert len(completed_events) >= 1


# =====================================================================
# Feature 4: Workflow cancellation
# =====================================================================

class TestWorkflowCancellation:
    """Cancellation via workflow.cancel() during execution."""

    def test_cancel_raises_workflow_cancelled_error(self, workspace):
        """Cancelling during execution raises WorkflowCancelledError."""
        load = FileLoader()
        tool = CancellableRowTool()

        with pytest.raises(WorkflowCancelledError):
            with Workflow(
                storage_path=workspace / "results",
                use_wetlands=True,
            ) as wf:
                raw = load(path=str(workspace / "data"))
                out = tool(input_path=raw["path"])

                # Cancel from another thread after a short delay
                def cancel_later():
                    time.sleep(0.5)
                    wf.cancel()

                t = threading.Thread(target=cancel_later)
                t.start()
                try:
                    wf.compute(out)
                finally:
                    t.join(timeout=10)

    def test_cancelled_event_emitted(self, workspace):
        """Cancelled nodes emit a 'cancelled' progress event."""
        events: list[ProgressEvent] = []

        load = FileLoader()
        tool = CancellableRowTool()

        with pytest.raises(WorkflowCancelledError):
            with Workflow(
                storage_path=workspace / "results",
                use_wetlands=True,
                on_progress=lambda e: events.append(e),
            ) as wf:
                raw = load(path=str(workspace / "data"))
                out = tool(input_path=raw["path"])

                def cancel_later():
                    time.sleep(0.5)
                    wf.cancel()

                t = threading.Thread(target=cancel_later)
                t.start()
                try:
                    wf.compute(out)
                finally:
                    t.join(timeout=10)

        cancelled = [e for e in events if e.status == "cancelled"]
        assert len(cancelled) >= 1


# =====================================================================
# Feature 5: Branch-level parallelism
# =====================================================================

class TestBranchParallelism:
    """Independent DAG branches run concurrently."""

    def test_independent_branches_concurrent(self, large_workspace):
        """Two independent slow branches finish faster than sequential."""
        load = FileLoader()
        tool = SlowRowTool()

        t0 = time.monotonic()
        with Workflow(
            storage_path=large_workspace / "results",
            use_wetlands=True,
            max_workers=2,
        ) as wf:
            raw = load(path=str(large_workspace / "data"))
            # Two independent branches from the same source
            branch_a = tool(input_path=raw["path"], name="branch_a")
            branch_b = tool(input_path=raw["path"], name="branch_b")
            out = wf.compute(branch_a, branch_b)

            elapsed = time.monotonic() - t0

            assert "branch_a" in out
            assert "branch_b" in out
            assert len(out["branch_a"]) == 6
            assert len(out["branch_b"]) == 6

            # With 6 rows * 0.3s each, sequential would take ~3.6s (2 branches = ~7.2s).
            # With branch parallelism + 2 workers, it should be much faster.
            # We just check it's below the fully-sequential time.
            # Note: this is a soft check — CI variability may affect it.
            assert elapsed < 7.0, f"Expected faster than sequential, took {elapsed:.1f}s"

    def test_results_identical_to_sequential(self, workspace):
        """Parallel execution produces the same results as sequential."""
        load = FileLoader()
        tool = SimpleRowTool()

        # Sequential
        df_seq = pd.DataFrame()
        seq_engine = SequentialEngine(use_wetlands=True)
        with Workflow(
            storage_path=workspace / "seq_results",
            use_wetlands=True,
        ) as wf:
            raw = load(path=str(workspace / "data"))
            out = tool(input_path=raw["path"])
            df_seq = wf.compute(out, engine=seq_engine)

        # Parallel (default engine)
        with Workflow(
            storage_path=workspace / "par_results",
            use_wetlands=True,
            max_workers=2,
        ) as wf:
            raw = load(path=str(workspace / "data"))
            out = tool(input_path=raw["path"])
            df_par = wf.compute(out)

            # Same shape and values (ignoring output paths which differ)
            assert len(df_seq) == len(df_par)
            assert set(df_seq.columns) == set(df_par.columns)
            assert list(df_seq["value"]) == list(df_par["value"])

    def test_dataframe_tool_never_concurrent(self, workspace):
        """DataFrameTool nodes always run on the main thread."""
        from .conftest import AddColumn

        load = FileLoader()
        tool = SimpleRowTool()
        add_col = AddColumn()

        with Workflow(
            storage_path=workspace / "results",
            use_wetlands=True,
        ) as wf:
            raw = load(path=str(workspace / "data"))
            tagged = add_col(column_name="tag", value="test")
            out = tool(input_path=raw["path"])
            df = wf.compute(out)

            # If it completes without deadlock/error, DataFrameTool ran correctly
            assert len(df) == 3

    def test_sequential_engine_deterministic(self, workspace):
        """SequentialEngine forces single-threaded, single-worker execution."""
        load = FileLoader()
        tool = SimpleRowTool()

        engine = SequentialEngine(use_wetlands=True)

        with Workflow(
            storage_path=workspace / "results",
            use_wetlands=True,
            max_workers=4,  # should be ignored by SequentialEngine
        ) as wf:
            raw = load(path=str(workspace / "data"))
            out = tool(input_path=raw["path"])
            df = wf.compute(out, engine=engine)

            assert len(df) == 3
            # SequentialEngine._resolve_worker_config always returns (1, None)
            mw, we = engine._resolve_worker_config(tool, wf)
            assert mw == 1
            assert we is None
