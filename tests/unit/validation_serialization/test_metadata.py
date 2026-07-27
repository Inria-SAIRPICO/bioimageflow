"""Focused tests split from ``tests/unit/test_validation_serialization.py``."""

# ruff: noqa: F401

import json

from enum import Enum

from pathlib import Path

from typing import Annotated, Any, Literal, Optional

import pytest

from bioimageflow_core import (
    Category,
    Connectable,
    EnvironmentSpec,
    GUIMeta,
    ImageShared,
    ImageSpec,
    IOModel,
    Layout,
    PathPicker,
    ProcessingTool,
    Semantic,
    Template,
)

from bioimageflow.dataframe_tool import Passthrough

from bioimageflow.validation import (
    SchemaSerializationError,
    _display_type_name,
    _extract_choices,
    _is_nullable,
    _jsonify_default,
    _serialize_connectable,
    serialize_input_schema,
    serialize_output_schema,
    serialize_resolved_outputs,
    serialize_tool_metadata,
)


from tests.testkit.validation_serialization import (
    FIXTURES_DIR,
    _canonical,
)


class TestSerializeToolMetadata:
    def test_files_is_source_dataframe_tool(self) -> None:
        from bioimageflow_common_tools import Files

        meta = serialize_tool_metadata(Files)
        assert meta == {
            "tool_type": "DataFrameTool",
            "accepts_upstream": False,
            "dynamic_outputs": False,
            "dataframe_output": True,
        }

    def test_generate_is_source(self) -> None:
        from bioimageflow_common_tools import Generate

        meta = serialize_tool_metadata(Generate)
        assert meta["tool_type"] == "DataFrameTool"
        assert meta["accepts_upstream"] is False

    def test_inner_join_accepts_upstream(self) -> None:
        from bioimageflow_common_tools import InnerJoin

        meta = serialize_tool_metadata(InnerJoin)
        assert meta["tool_type"] == "DataFrameTool"
        assert meta["accepts_upstream"] is True
        # InnerJoin overrides resolve_merge_schema → dynamic_outputs=True.
        assert meta["dynamic_outputs"] is True

    def test_processing_tool_metadata(self) -> None:
        from bioimageflow_common_tools.connected_components import ConnectedComponents

        meta = serialize_tool_metadata(ConnectedComponents)
        assert meta["tool_type"] == "ProcessingTool"
        assert meta["accepts_upstream"] is True
        assert meta["dynamic_outputs"] is False
        assert meta["dataframe_output"] is True

    def test_metadata_is_json_safe(self) -> None:
        from bioimageflow_common_tools import Files

        json.dumps(serialize_tool_metadata(Files))

    def test_dynamic_outputs_true_for_generate(self) -> None:
        from bioimageflow_common_tools import Generate

        assert serialize_tool_metadata(Generate)["dynamic_outputs"] is True

    def test_dynamic_outputs_false_for_files(self) -> None:
        from bioimageflow_common_tools import Files

        assert serialize_tool_metadata(Files)["dynamic_outputs"] is False

    def test_dynamic_outputs_true_for_merge_tools(self) -> None:
        """Merge tools override resolve_merge_schema → dynamic_outputs is True."""
        from bioimageflow_common_tools import (
            Collect,
            Concat,
            CrossJoin,
            InnerJoin,
            JoinOnColumn,
        )

        for tool_cls in (InnerJoin, CrossJoin, JoinOnColumn, Concat, Collect):
            assert serialize_tool_metadata(tool_cls)["dynamic_outputs"] is True, (
                f"{tool_cls.__name__} should report dynamic_outputs=True "
                f"because it overrides resolve_merge_schema"
            )


class TestSerializeResolvedOutputs:
    def test_unresolved_when_inputs_missing(self, tmp_path) -> None:
        from bioimageflow import Workflow
        from bioimageflow.dataframe_tool import DataFrameTool

        class Unconfigured(DataFrameTool):
            display_name = "Unconfigured"

            class Inputs(IOModel):
                pass

            @classmethod
            def resolve_outputs(cls, inputs=None):
                return None

        with Workflow(storage_path=tmp_path, engine="direct"):
            n = Unconfigured()()
            out = serialize_resolved_outputs(n)
            assert out == {"resolved": False, "columns": {}}

    def test_resolved_after_inputs_set(self, tmp_path) -> None:
        from bioimageflow import Workflow
        from bioimageflow_common_tools import Generate

        with Workflow(storage_path=tmp_path, engine="direct"):
            g = Generate()(column_name="sensitivity", values=[1, 2])
            out = serialize_resolved_outputs(g)
            assert out["resolved"] is True
            assert "sensitivity" in out["columns"]

    def test_json_safe(self, tmp_path) -> None:
        from bioimageflow import Workflow
        from bioimageflow_common_tools import Generate

        with Workflow(storage_path=tmp_path, engine="direct"):
            g = Generate()(column_name="x", values=[1])
            json.dumps(serialize_resolved_outputs(g))


def test_snapshot_connected_components() -> None:
    from bioimageflow_common_tools.connected_components import ConnectedComponents

    actual = {
        "inputs": serialize_input_schema(ConnectedComponents),
        "outputs": serialize_output_schema(ConnectedComponents),
    }
    expected = json.loads(
        (FIXTURES_DIR / "connected_components_schema.json").read_text()
    )
    assert _canonical(actual) == _canonical(expected)
