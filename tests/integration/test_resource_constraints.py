"""
Test resource constraint declarations.

Covers:
- ResourceSpec declaration on ProcessingTool
- Default resource values
- Sequential engine ignores resource specs
- Resource declarations are accessible on tool instances
"""

import pytest

from bioimageflow_core import ResourceSpec

from .conftest import StubBatchProcessor, StubSegmenter


class TestResourceSpec:

    def test_default_values(self):
        spec = ResourceSpec()
        assert spec.cpu == 1
        assert spec.gpu == 0
        assert spec.gpu_memory is None
        assert spec.max_concurrent == 0
        assert spec.memory is None

    def test_custom_values(self):
        spec = ResourceSpec(gpu=2, gpu_memory="16GB", max_concurrent=4, memory="32GB")
        assert spec.gpu == 2
        assert spec.gpu_memory == "16GB"
        assert spec.max_concurrent == 4
        assert spec.memory == "32GB"

    def test_frozen(self):
        spec = ResourceSpec(gpu=1)
        with pytest.raises(AttributeError):
            spec.gpu = 2  # type: ignore[reportAttributeAccessIssue]


class TestToolResourceDeclaration:

    def test_batch_processor_has_resources(self):
        tool = StubBatchProcessor()
        assert tool.resources.gpu == 1
        assert tool.resources.max_concurrent == 2

    def test_tool_without_resources(self):
        tool = StubSegmenter()
        assert not hasattr(tool, "resources") or tool.resources is None

    def test_resources_accessible_for_engine(self):
        """Engine can inspect resource requirements from tool instances."""
        tool = StubBatchProcessor()
        spec = tool.resources
        assert isinstance(spec, ResourceSpec)


class TestSequentialEngineIgnoresResources:

    def test_gpu_tool_runs_on_sequential_engine(self, tmp_workspace):
        """Sequential engine runs GPU tools without GPU (ignores resource spec)."""
        from bioimageflow import Workflow
        from .conftest import FileLoader

        load = FileLoader()
        batch = StubBatchProcessor()

        with Workflow(
            storage_path=tmp_workspace / "results", engine="sequential"
        ) as wf:
            raw = load(path=str(tmp_workspace / "data"))
            embeddings = batch(input_image=raw["path"])
            df = wf.compute(embeddings)

            assert len(df) == 3
