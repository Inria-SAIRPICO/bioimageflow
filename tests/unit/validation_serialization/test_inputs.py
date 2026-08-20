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
    RowConsumption,
    Semantic,
    Template,
)

from bioimageflow.dataframe_tool import DataFrameTool, Passthrough

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
    _ENV,
    _ImageFieldGuiTool,
    _SchemaTool,
)


class TestSerializeInputSchema:
    def test_keys_present(self) -> None:
        schema = serialize_input_schema(_SchemaTool)
        expected_keys = {
            "type",
            "required",
            "nullable",
            "connectable",
            "default",
            "display_name",
            "description",
            "group",
            "min",
            "max",
            "step",
            "path_picker",
            "choices",
            "image_spec",
        }
        for field in schema.values():
            assert set(field.keys()) == expected_keys

    def test_required_field(self) -> None:
        schema = serialize_input_schema(_SchemaTool)
        entry = schema["input_image"]
        assert entry["required"] is True
        assert entry["default"] is None
        assert entry["type"] == "ImageFile"
        assert entry["connectable"] == "by_default"
        assert entry["display_name"] == "Input image"
        assert entry["description"] == "A 2D intensity image."
        assert entry["image_spec"] is not None
        assert "intensity" in entry["image_spec"]["semantics"]
        assert entry["choices"] is None
        assert entry["path_picker"] is None

    def test_path_picker_modes(self) -> None:
        class PickerTool(ProcessingTool):
            row_consumption = RowConsumption.MAPPED
            environment = _ENV

            class Inputs(IOModel):
                file_path: Annotated[Path, GUIMeta(path_picker=PathPicker.FILE)]
                folder_path: Annotated[Path, GUIMeta(path_picker=PathPicker.FOLDER)]
                either_path: Annotated[Path, GUIMeta(path_picker=PathPicker.BOTH)]

            class Outputs(IOModel):
                result: int

            def process_row(self, arguments, *, context: object | None = None):
                return self.Outputs(result=0)

        schema = serialize_input_schema(PickerTool)

        assert schema["file_path"]["path_picker"] == "file"
        assert schema["folder_path"]["path_picker"] == "folder"
        assert schema["either_path"]["path_picker"] == "both"

    def test_numeric_field_gui_meta(self) -> None:
        schema = serialize_input_schema(_SchemaTool)
        entry = schema["diameter"]
        assert entry["type"] == "float"
        assert entry["required"] is False
        assert entry["default"] == 1.0
        assert entry["min"] == 0.0
        assert entry["max"] == 100.0
        assert entry["step"] == 0.1
        assert entry["connectable"] == "not_by_default"
        assert entry["image_spec"] is None
        assert entry["choices"] is None

    def test_literal_choices(self) -> None:
        schema = serialize_input_schema(_SchemaTool)
        entry = schema["model"]
        assert entry["type"] == "str"
        assert entry["choices"] == ["fast", "accurate"]
        assert entry["group"] == "advanced"
        assert entry["default"] == "fast"

    def test_enum_choices_and_default(self) -> None:
        schema = serialize_input_schema(_SchemaTool)
        entry = schema["mode_enum"]
        assert entry["type"] == "str"
        assert entry["choices"] == ["fast", "accurate"]
        assert entry["default"] == "fast"

    def test_bare_int(self) -> None:
        schema = serialize_input_schema(_SchemaTool)
        entry = schema["count"]
        assert entry["type"] == "int"
        assert entry["required"] is False
        assert entry["nullable"] is False
        assert entry["default"] == 5
        assert entry["display_name"] is None
        assert entry["image_spec"] is None

    def test_required_field_not_nullable(self) -> None:
        schema = serialize_input_schema(_SchemaTool)
        # input_image is required and non-Optional → nullable: False.
        assert schema["input_image"]["nullable"] is False

    def test_optional_with_default_none(self) -> None:
        schema = serialize_input_schema(_SchemaTool)
        entry = schema["area_lim"]
        # `Optional[X]` is unwrapped for type display, but `nullable` surfaces
        # the None-ness so the GUI can offer a "set to null" toggle.
        assert entry["type"] == "float"
        assert entry["required"] is False
        assert entry["nullable"] is True
        assert entry["default"] is None

    def test_optional_required(self) -> None:
        schema = serialize_input_schema(_SchemaTool)
        entry = schema["threshold"]
        # `int | None` with no class-level default — user must pass something,
        # but None is an acceptable value.
        assert entry["type"] == "int"
        assert entry["required"] is True
        assert entry["nullable"] is True
        assert entry["default"] is None

    def test_tool_with_no_inputs_class(self) -> None:
        class NoInputs:
            pass

        assert serialize_input_schema(NoInputs) == {}  # type: ignore[arg-type]

    def test_json_serializable(self) -> None:
        schema = serialize_input_schema(_SchemaTool)
        # Must not raise
        json.dumps(schema)

    def test_required_is_orthogonal_to_optional(self) -> None:
        """Optional[X] without a default is still required."""

        class OptTool(ProcessingTool):
            row_consumption = RowConsumption.MAPPED
            environment = _ENV

            class Inputs(IOModel):
                maybe_int: int | None  # no default → required

            class Outputs(IOModel):
                out: int

            def process_row(self, arguments, *, context: object | None = None):
                return self.Outputs(out=0)

        schema = serialize_input_schema(OptTool)
        entry = schema["maybe_int"]
        assert entry["required"] is True
        assert entry["type"] == "int"

    def test_image_path_gui_meta_preserved(self) -> None:
        schema = serialize_input_schema(_ImageFieldGuiTool)
        entry = schema["input_image"]
        assert entry["type"] == "ImageFile"
        assert entry["connectable"] == "by_default"
        assert entry["display_name"] == "Input image"
        assert entry["description"] == "A 2D intensity image."
        assert entry["group"] == "data"
        assert entry["image_spec"] == {
            "semantics": ["intensity"],
            "layouts": ["YX"],
            "dtypes": [],
            "formats": [],
        }
