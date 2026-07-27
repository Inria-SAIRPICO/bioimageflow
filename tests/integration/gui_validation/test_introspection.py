"""Focused tests split from ``tests/integration/test_gui_validation_api.py``."""

# ruff: noqa: F401

import json

from pathlib import Path

from typing import Annotated, Any

import pytest

from pydantic import Field

from bioimageflow_core import (
    Arguments,
    EnvironmentSpec,
    IOModel,
    ImageSpec,
    ProcessingTool,
    Semantic,
    Layout,
    Template,
)

from bioimageflow import (
    NodePlan,
    SourceToolUpstreamError,
    ValidationError,
    Workflow,
    get_inputs_schema,
    serialize_image_spec,
    serialize_resolved_outputs,
    serialize_tool_metadata,
    topological_order,
    validate_parameters,
)

from bioimageflow.node import BindingError, ColumnNotFoundError, IndexAlignmentError

from tests.testkit.integration_tools import (
    FileLoader,
    StubSegmenter,
    StubStats,
)


class TestIntrospectionHelpers:
    def test_topological_order_method(self, tmp_path) -> None:
        wf = Workflow(storage_path=tmp_path, engine="direct")
        with wf:
            load = FileLoader()(path="/tmp/x")
            seg = StubSegmenter()(input_image=load["path"])
            StubStats()(image=load["path"], mask=seg["mask"])
        order = wf.topological_order()
        assert order.index("FileLoader_1") < order.index("StubSegmenter_1")
        assert order.index("StubSegmenter_1") < order.index("StubStats_1")

    def test_downstream_of(self, tmp_path) -> None:
        wf = Workflow(storage_path=tmp_path, engine="direct")
        with wf:
            load = FileLoader()(path="/tmp/x")
            seg = StubSegmenter()(input_image=load["path"])
            StubStats()(image=load["path"], mask=seg["mask"])
        assert wf.downstream_of("FileLoader_1") == {"StubSegmenter_1", "StubStats_1"}
        assert wf.downstream_of("StubStats_1") == set()

    def test_downstream_of_unknown(self, tmp_path) -> None:
        wf = Workflow(storage_path=tmp_path, engine="direct")
        with pytest.raises(KeyError):
            wf.downstream_of("nope")

    def test_serialize_image_spec_shape(self) -> None:
        spec = ImageSpec(
            semantics={Semantic.INTENSITY, Semantic.LABEL},
            layouts={Layout.PLANAR},
            dtypes={"uint8"},
            formats={"tif"},
        )
        out = serialize_image_spec(spec)
        assert out == {
            "semantics": ["intensity", "label"],
            "layouts": ["YX"],
            "dtypes": ["uint8"],
            "formats": ["tif"],
        }

    def test_serialize_image_spec_none(self) -> None:
        assert serialize_image_spec(None) is None

    @pytest.mark.compat
    def test_get_inputs_schema_has_serialized_key(self) -> None:
        schema = get_inputs_schema(StubSegmenter())
        entry = schema["input_image"]
        assert "image_spec_serialized" in entry
        assert entry["image_spec_serialized"]["semantics"] == ["intensity"]

    def test_serialize_tool_metadata_files(self) -> None:
        """Wire-format parity test for the platform-consumed tool metadata."""
        from bioimageflow_common_tools import Files

        meta = serialize_tool_metadata(Files)
        assert meta["tool_type"] == "DataFrameTool"
        assert meta["accepts_upstream"] is False
        assert meta["dataframe_output"] is True
        json.dumps(meta)  # JSON-safe

    def test_serialize_tool_metadata_processing_tool(self) -> None:
        meta = serialize_tool_metadata(StubSegmenter)
        assert meta["tool_type"] == "ProcessingTool"
        assert meta["accepts_upstream"] is True
        assert meta["dataframe_output"] is True

    def test_serialize_tool_metadata_merge_tool(self) -> None:
        from bioimageflow_common_tools import CrossJoin

        meta = serialize_tool_metadata(CrossJoin)
        assert meta["tool_type"] == "DataFrameTool"
        assert meta["accepts_upstream"] is True
        # Merge tool — resolved schema depends on upstreams, so the GUI
        # must call serialize_resolved_outputs to render per-column pins.
        assert meta["dynamic_outputs"] is True


class TestSourceToolUpstream:
    def test_source_tool_with_upstream_raises(self, tmp_path: Path) -> None:
        from bioimageflow_common_tools import Files

        wf = Workflow(engine="direct", storage_path=tmp_path)
        with wf:
            other = Files()(path=str(tmp_path))
            with pytest.raises(SourceToolUpstreamError):
                Files()(other, path=str(tmp_path))

    def test_source_tool_kwargs_only_works(self, tmp_path: Path) -> None:
        from bioimageflow_common_tools import Files

        wf = Workflow(engine="direct", storage_path=tmp_path)
        with wf:
            Files()(path=str(tmp_path))
        # No exception → the workflow built fine.
        assert "Files_1" in wf._nodes


class TestSerializeResolvedOutputsWireFormat:
    """Parity tests for the wire-format the platform consumes for resolved
    output pins on configured nodes.
    """

    def test_unconfigured_generate_has_no_columns(self, tmp_path: Path) -> None:
        # Generate without column_name can't even be constructed (required
        # field). Cover the unresolved path with a custom DataFrameTool below.
        from bioimageflow.dataframe_tool import DataFrameTool

        class Dyn(DataFrameTool):
            display_name = "Dyn"

            class Inputs(IOModel):
                pass

            @classmethod
            def resolve_outputs(cls, inputs=None):
                return None

        wf = Workflow(engine="direct", storage_path=tmp_path)
        with wf:
            n = Dyn()()
            out = serialize_resolved_outputs(n)
            assert out == {"resolved": False, "columns": {}}
            json.dumps(out)

    def test_generate_resolved_after_column_name(self, tmp_path: Path) -> None:
        from bioimageflow_common_tools import Generate

        wf = Workflow(engine="direct", storage_path=tmp_path)
        with wf:
            g = Generate()(column_name="sensitivity", values=[1, 2, 3])
            out = serialize_resolved_outputs(g)
            assert out["resolved"] is True
            assert set(out["columns"].keys()) == {"sensitivity"}
            json.dumps(out)

    def test_cross_join_resolved_schema_for_parameter_space(
        self, tmp_path: Path
    ) -> None:
        """parameter_space_exploration's exact wiring resolves at construction."""
        from bioimageflow_common_tools import CrossJoin, Files, Generate

        wf = Workflow(engine="direct", storage_path=tmp_path)
        with wf:
            files = Files()(path=str(tmp_path))
            sens = Generate()(column_name="sensitivity", values=[0.1, 0.2])
            size = Generate()(column_name="size", values=[1, 2])
            grid = CrossJoin()(files, sens, size)
            out = serialize_resolved_outputs(grid)
            assert out["resolved"] is True
            assert set(out["columns"].keys()) == {"path", "sensitivity", "size"}
            json.dumps(out)
