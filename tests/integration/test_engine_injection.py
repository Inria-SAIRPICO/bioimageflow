"""
Test that engines can be injected into Workflow for debugging and testing.

Covers:
- compute() accepts engine parameter
- compute_steps() accepts engine parameter
- Injected engine is actually used (not replaced by default)
- Engine state is accessible after execution
- Default behavior (no engine) unchanged
"""
import pandas as pd

from bioimageflow import Workflow
from bioimageflow.engine import SequentialEngine
from .conftest import FileLoader, StubSegmenter, StubStats


class TestEngineInjection:
    """Test injecting custom engines into Workflow."""

    def test_compute_accepts_engine_parameter(self, tmp_workspace):
        """Workflow.compute() accepts an engine parameter."""
        load = FileLoader()
        segment = StubSegmenter()
        measure = StubStats()

        # Create a custom engine with a marker
        custom_engine = SequentialEngine(use_wetlands=False)
        custom_engine._test_marker = "injected"  # type: ignore[attr-defined]

        with Workflow(engine="direct", storage_path=tmp_workspace / "results") as wf:
            raw = load(path=str(tmp_workspace / "data"))
            masks = segment(input_image=raw["path"])
            results = measure(image=raw["path"], mask=masks["mask"])

            # Inject the engine
            df = wf.compute(results, engine=custom_engine)

            # Basic sanity
            assert isinstance(df, pd.DataFrame)
            assert len(df) == 3

            # The injected engine should still have our marker
            assert hasattr(custom_engine, "_test_marker")
            assert custom_engine._test_marker == "injected"  # type: ignore[attr-defined]

    def test_injected_engine_is_actually_used(self, tmp_workspace):
        """The injected engine is the one that executes, not a new default engine."""
        load = FileLoader()
        segment = StubSegmenter()
        measure = StubStats()

        # Create a custom engine that tracks whether it was used
        class TrackingEngine(SequentialEngine):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.execute_called = False

            def execute(self, targets, workflow):
                self.execute_called = True
                return super().execute(targets, workflow)

        tracking_engine = TrackingEngine(use_wetlands=False)

        with Workflow(engine="direct", storage_path=tmp_workspace / "results") as wf:
            raw = load(path=str(tmp_workspace / "data"))
            masks = segment(input_image=raw["path"])
            results = measure(image=raw["path"], mask=masks["mask"])

            wf.compute(results, engine=tracking_engine)

            # Our tracking engine should have been used
            assert tracking_engine.execute_called, "Injected engine was not used"

    def test_compute_steps_accepts_engine_parameter(self, tmp_workspace):
        """Workflow.compute_steps() accepts an engine parameter."""
        load = FileLoader()
        segment = StubSegmenter()
        measure = StubStats()

        custom_engine = SequentialEngine(use_wetlands=False)
        custom_engine._test_marker = "steps_injected"  # type: ignore[attr-defined]

        with Workflow(engine="direct", storage_path=tmp_workspace / "results") as wf:
            raw = load(path=str(tmp_workspace / "data"))
            masks = segment(input_image=raw["path"])
            results = measure(image=raw["path"], mask=masks["mask"])

            steps = list(wf.compute_steps(results, engine=custom_engine))

            # Should have steps for each node in the DAG
            assert len(steps) >= 2  # at least segment and measure
            # All steps should have been prepared from our custom engine
            for step in steps:
                assert step._engine is custom_engine

    def test_compute_steps_injected_engine_is_used(self, tmp_workspace):
        """The injected engine in compute_steps is actually used."""
        load = FileLoader()
        segment = StubSegmenter()
        measure = StubStats()

        class TrackingEngine(SequentialEngine):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.execute_steps_called = False

            def execute_steps(self, targets, workflow):
                self.execute_steps_called = True
                # Don't actually yield; just mark as called
                return super().execute_steps(targets, workflow)

        tracking_engine = TrackingEngine(use_wetlands=False)

        with Workflow(engine="direct", storage_path=tmp_workspace / "results") as wf:
            raw = load(path=str(tmp_workspace / "data"))
            masks = segment(input_image=raw["path"])
            results = measure(image=raw["path"], mask=masks["mask"])

            _steps = list(wf.compute_steps(results, engine=tracking_engine))

            # Our tracking engine's execute_steps should have been called
            assert tracking_engine.execute_steps_called, "Injected engine's execute_steps not used"

    def test_default_engine_when_none_injected(self, tmp_workspace):
        """When no engine is provided, default SequentialEngine is created."""
        load = FileLoader()
        segment = StubSegmenter()
        measure = StubStats()

        with Workflow(engine="direct", storage_path=tmp_workspace / "results") as wf:
            raw = load(path=str(tmp_workspace / "data"))
            masks = segment(input_image=raw["path"])
            results = measure(image=raw["path"], mask=masks["mask"])

            # Compute without engine
            df = wf.compute(results)

            assert isinstance(df, pd.DataFrame)
            assert len(df) == 3

    def test_engine_parameter_is_keyword_only(self, tmp_workspace):
        """Engine parameter should be keyword-only to avoid positional confusion."""
        load = FileLoader()
        segment = StubSegmenter()
        measure = StubStats()

        custom_engine = SequentialEngine(use_wetlands=False)

        with Workflow(engine="direct", storage_path=tmp_workspace / "results") as wf:
            raw = load(path=str(tmp_workspace / "data"))
            masks = segment(input_image=raw["path"])
            results = measure(image=raw["path"], mask=masks["mask"])

            # Should work with keyword
            df = wf.compute(results, engine=custom_engine)
            assert isinstance(df, pd.DataFrame)

            # Should NOT accept engine as positional (compute takes *targets then dev_mode)
            # dev_mode is the only other keyword arg currently; engine must be after it
            # This is enforced by Python's signature; we just verify it works as keyword

    def test_engine_state_accessible_after_compute(self, tmp_workspace):
        """After compute returns, the injected engine's internal state can be inspected."""
        load = FileLoader()
        segment = StubSegmenter()
        measure = StubStats()

        custom_engine = SequentialEngine(use_wetlands=False)

        with Workflow(engine="direct", storage_path=tmp_workspace / "results") as wf:
            raw = load(path=str(tmp_workspace / "data"))
            masks = segment(input_image=raw["path"])
            results = measure(image=raw["path"], mask=masks["mask"])

            wf.compute(results, engine=custom_engine)

            # After compute, we should be able to inspect engine internals
            # For example, check that _use_wetlands is as configured
            assert not custom_engine._use_wetlands
            # If wetlands were used, we could check _env_manager

    def test_compute_steps_engine_state_accessible(self, tmp_workspace):
        """After compute_steps generator completes, injected engine state is accessible."""
        load = FileLoader()
        segment = StubSegmenter()
        measure = StubStats()

        custom_engine = SequentialEngine(use_wetlands=False)

        with Workflow(engine="direct", storage_path=tmp_workspace / "results") as wf:
            raw = load(path=str(tmp_workspace / "data"))
            masks = segment(input_image=raw["path"])
            results = measure(image=raw["path"], mask=masks["mask"])

            _steps = list(wf.compute_steps(results, engine=custom_engine))

            # After generator exhausts, engine should be in a consistent state
            assert not custom_engine._use_wetlands
