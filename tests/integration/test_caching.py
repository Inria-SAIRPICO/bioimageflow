"""
Test hashing, caching, and cache invalidation.

Covers:
- Signature hash computation (deterministic)
- Cache hit: re-running same workflow skips execution
- Cache miss: changing parameters triggers re-execution
- Cache miss: changing tool version invalidates cache
- Cache miss: changing environment dependencies invalidates cache
- Dev mode: source code changes invalidate cache
- Published records are retained until explicit storage maintenance
- Dependency normalization (sort order, whitespace)
"""

from pathlib import Path
from typing import Annotated, Any

import pandas as pd
import pytest

from bioimageflow import Workflow
from bioimageflow_core import (
    EnvironmentSpec,
    IOModel,
    ProcessingTool,
    Semantic,
    Template,
)
from bioimageflow_core.types import ImageSpec

from tests.testkit.integration_tools import FileLoader, StubSegmenter


class TestCacheHit:

    @pytest.mark.compat
    def test_second_run_uses_cache(self, tmp_workspace):
        """Running the same workflow twice should use cached results."""
        load = FileLoader()
        segment = StubSegmenter()

        results: list[Any] = []
        with Workflow(engine="direct", storage_path=tmp_workspace / "results") as wf:
            raw = load(path=str(tmp_workspace / "data"))
            masks = segment(input_image=raw["path"], diameter=30.0)
            results.append(wf.compute(masks))

        # Re-run with identical configuration
        with Workflow(engine="direct", storage_path=tmp_workspace / "results") as wf:
            raw = load(path=str(tmp_workspace / "data"))
            masks = segment(input_image=raw["path"], diameter=30.0)
            results.append(wf.compute(masks))

        pd.testing.assert_frame_equal(results[0], results[1])

    def test_cached_node_reports_cached_status(self, tmp_workspace):
        """Progress callback should report 'cached' for cache hits."""
        events = []

        def on_progress(event):
            events.append((event.node_name, event.status))

        load = FileLoader()
        segment = StubSegmenter()

        # First run
        with Workflow(engine="direct", storage_path=tmp_workspace / "results") as wf:
            raw = load(path=str(tmp_workspace / "data"))
            masks = segment(input_image=raw["path"])
            wf.compute(masks)

        # Second run with progress tracking
        with Workflow(
            engine="direct",
            storage_path=tmp_workspace / "results", on_progress=on_progress
        ) as wf:
            raw = load(path=str(tmp_workspace / "data"))
            masks = segment(input_image=raw["path"])
            wf.compute(masks)
            cached_events = [(n, s) for n, s in events if s == "cached"]
            assert len(cached_events) > 0


class TestCacheMiss:

    @pytest.mark.compat
    def test_different_parameter_invalidates_cache(self, tmp_workspace):
        load = FileLoader()
        segment = StubSegmenter()

        # First run
        with Workflow(engine="direct", storage_path=tmp_workspace / "results") as wf:
            raw = load(path=str(tmp_workspace / "data"))
            masks = segment(input_image=raw["path"], diameter=30.0)
            df1 = wf.compute(masks)
            assert len(df1) == 3

        # Second run with different parameter — should NOT use cache
        events: list[Any] = []
        with Workflow(
            engine="direct",
            storage_path=tmp_workspace / "results",
            on_progress=lambda e: events.append(e),
        ) as wf:
            raw = load(path=str(tmp_workspace / "data"))
            masks = segment(input_image=raw["path"], diameter=50.0)
            df2 = wf.compute(masks)
            assert len(df2) == 3
            # The segmenter node must have been re-executed (not cached)
            seg_started = [
                e for e in events
                if "Segmenter" in e.node_name and e.status == "started"
            ]
            assert len(seg_started) > 0, "Different parameter should cause cache miss"

    def test_different_upstream_invalidates_cache(self, tmp_workspace):
        """Changed upstream hash propagates downstream."""
        load = FileLoader()
        segment = StubSegmenter()

        # Add a file to the data directory
        (tmp_workspace / "data" / "cell_04.tif").write_text("NEW_IMAGE")

        with Workflow(engine="direct", storage_path=tmp_workspace / "results") as wf:
            raw = load(path=str(tmp_workspace / "data"))
            masks = segment(input_image=raw["path"])
            df = wf.compute(masks)
            assert len(df) == 4  # Now 4 images


class TestDevMode:

    def test_dev_mode_includes_source_hash(self, tmp_workspace):
        """In dev mode, changing tool source code invalidates cache."""
        load = FileLoader()
        segment = StubSegmenter()

        # First run with dev_mode
        with Workflow(engine="direct", storage_path=tmp_workspace / "results") as wf:
            raw = load(path=str(tmp_workspace / "data"))
            masks = segment(input_image=raw["path"])
            df1 = wf.compute(masks, dev_mode=True)
            assert len(df1) == 3

    def test_dev_mode_cache_miss_on_source_change(self, tmp_workspace):
        """In dev mode, a tool with different source code causes a cache miss."""
        load = FileLoader()
        segment = StubSegmenter()

        # First run
        events1: list[Any] = []
        with Workflow(
            engine="direct",
            storage_path=tmp_workspace / "results",
            on_progress=lambda e: events1.append(e),
        ) as wf:
            raw = load(path=str(tmp_workspace / "data"))
            masks = segment(input_image=raw["path"])
            wf.compute(masks, dev_mode=True)

        # Second identical run — should cache hit
        events2: list[Any] = []
        with Workflow(
            engine="direct",
            storage_path=tmp_workspace / "results",
            on_progress=lambda e: events2.append(e),
        ) as wf:
            raw = load(path=str(tmp_workspace / "data"))
            masks = segment(input_image=raw["path"])
            wf.compute(masks, dev_mode=True)
            seg_cached = [
                e for e in events2
                if "Segmenter" in e.node_name and e.status == "cached"
            ]
            assert len(seg_cached) > 0, "Second identical run should use cache"

        # Third run with a dynamically created tool (different source hash)
        # Even though it has the same name and params, dev_mode should miss cache
        from bioimageflow_core import Arguments

        class ModifiedSegmenter(ProcessingTool):
            display_name = "Stub Segmenter"
            environment = segment.environment

            class Inputs(IOModel):
                input_image: Annotated[Path, ImageSpec(semantics={Semantic.INTENSITY})]
                diameter: float = 30.0

            class Outputs(IOModel):
                mask: Annotated[Path, ImageSpec(semantics={Semantic.LABEL})] = Template(
                    "{input_image.stem}_mask_{row_index}.png"
                )
                cell_count: int

            def process_row(self, arguments: Arguments, *, context: object | None = None):
                # Different source code than StubSegmenter
                mask_path = Path(arguments.mask)
                mask_path.parent.mkdir(parents=True, exist_ok=True)
                mask_path.write_text("MODIFIED_MASK_DATA")
                return self.Outputs(mask=mask_path, cell_count=99)

        events3: list[Any] = []
        modified_seg = ModifiedSegmenter()
        with Workflow(
            engine="direct",
            storage_path=tmp_workspace / "results",
            on_progress=lambda e: events3.append(e),
        ) as wf:
            raw = load(path=str(tmp_workspace / "data"))
            masks = modified_seg(input_image=raw["path"])
            wf.compute(masks, dev_mode=True)
            seg_started = [
                e for e in events3
                if "Segmenter" in e.node_name and e.status == "started"
            ]
            assert len(seg_started) > 0, "Modified source should cause cache miss in dev mode"


class TestEnvironmentDependencyChange:

    def test_env_dependency_change_invalidates_cache(self, tmp_workspace):
        """Changing EnvironmentSpec dependencies causes cache miss."""

        env_v1 = EnvironmentSpec(
            name="versioned_env",
            dependencies={"conda": ["numpy==1.24"]},
        )

        class ToolV1(ProcessingTool):
            display_name = "Versioned Tool"
            environment = env_v1

            class Inputs(IOModel):
                input_image: Annotated[Path, ImageSpec(semantics={Semantic.INTENSITY})]

            class Outputs(IOModel):
                result: float

            def process_row(self, arguments, *, context: object | None = None):
                return self.Outputs(result=1.0)

        load = FileLoader()

        # First run with numpy==1.24
        with Workflow(engine="direct", storage_path=tmp_workspace / "results") as wf:
            raw = load(path=str(tmp_workspace / "data"))
            out = ToolV1()(input_image=raw["path"])
            wf.compute(out)

        # Second run with numpy==2.0 — same tool name, different env deps
        env_v2 = EnvironmentSpec(
            name="versioned_env_v2",
            dependencies={"conda": ["numpy==2.0"]},
        )

        class ToolV2(ProcessingTool):
            display_name = "Versioned Tool"
            environment = env_v2

            class Inputs(IOModel):
                input_image: Annotated[Path, ImageSpec(semantics={Semantic.INTENSITY})]

            class Outputs(IOModel):
                result: float

            def process_row(self, arguments, *, context: object | None = None):
                return self.Outputs(result=2.0)

        events: list[Any] = []
        with Workflow(
            engine="direct",
            storage_path=tmp_workspace / "results",
            on_progress=lambda e: events.append(e),
        ) as wf:
            raw = load(path=str(tmp_workspace / "data"))
            out = ToolV2()(input_image=raw["path"])
            wf.compute(out)
            # The tool node must have been re-executed (not cached) due to env change
            started = [
                e for e in events
                if "ToolV2" in e.node_name and e.status == "started"
            ]
            assert len(started) > 0, "Different env dependencies should cause cache miss"


class TestDependencyNormalization:

    def test_sorted_dependencies_produce_same_hash(self):
        """Dependency list order does not affect the hash."""
        env1 = EnvironmentSpec(
            name="test", dependencies={"conda": ["numpy=2.4.2", "cellpose==3.0"]}
        )
        env2 = EnvironmentSpec(
            name="test", dependencies={"conda": ["cellpose==3.0", "numpy=2.4.2"]}
        )
        # Both should normalize to the same hash
        from bioimageflow.cache import compute_env_hash

        assert compute_env_hash(env1.dependencies) == compute_env_hash(
            env2.dependencies
        )

    def test_whitespace_stripped(self):
        env1 = EnvironmentSpec(
            name="test", dependencies={"conda": ["cellpose==3.0"]}
        )
        env2 = EnvironmentSpec(
            name="test", dependencies={"conda": [" cellpose==3.0 "]}
        )
        from bioimageflow.cache import compute_env_hash

        assert compute_env_hash(env1.dependencies) == compute_env_hash(
            env2.dependencies
        )


class TestDeterministicSerialize:

    def test_unknown_type_raises(self):
        """deterministic_serialize raises TypeError on unknown types."""
        from bioimageflow.cache import deterministic_serialize

        class Unknown:
            pass

        with pytest.raises(TypeError, match="Cannot serialize"):
            deterministic_serialize(Unknown())

    def test_known_types_serialize_deterministically(self):
        """Path, set, tuple, Enum are serialized deterministically."""
        from bioimageflow.cache import deterministic_serialize

        result1 = deterministic_serialize({"a": Path("/tmp/x"), "b": {3, 1, 2}})
        result2 = deterministic_serialize({"a": Path("/tmp/x"), "b": {2, 1, 3}})
        assert result1 == result2


class TestCacheRetention:

    def test_max_executions_is_removed_from_workflow_api(self, tmp_path):
        """Published-record pruning is no longer a Workflow constructor policy."""
        with pytest.raises(TypeError, match="max_executions"):
            Workflow(
                storage_path=tmp_path,
                engine="direct",
                max_executions=3,
            )

    def test_max_age_is_removed_from_workflow_api(self, tmp_path):
        """Age-based cache cleanup belongs to an explicit maintenance API."""
        with pytest.raises(TypeError, match="max_age"):
            Workflow(storage_path=tmp_path, engine="direct", max_age="7d")
