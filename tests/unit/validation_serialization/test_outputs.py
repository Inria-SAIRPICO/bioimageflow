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
    _all_common_tool_classes,
)


class TestSerializeOutputSchema:
    def test_basic(self) -> None:
        schema = serialize_output_schema(_SchemaTool)
        assert set(schema.keys()) == {"mask", "cell_count"}

        mask = schema["mask"]
        assert mask["type"] == "ImageFile"
        assert mask["default"] == "{input_image.stem}_mask{ext}"
        assert mask["template"] == "{input_image.stem}_mask{ext}"
        assert mask["image_spec"] is not None
        assert "label" in mask["image_spec"]["semantics"]

        count = schema["cell_count"]
        assert count["type"] == "int"
        assert count["default"] is None
        assert count["image_spec"] is None

    def test_no_outputs_returns_empty(self) -> None:
        class NoOutputs:
            pass

        assert serialize_output_schema(NoOutputs) == {}  # type: ignore[arg-type]

    def test_passthrough_marker(self) -> None:
        class Collect(DataFrameTool):
            display_name = "Collect"
            category = Category.UTILITIES

            class Inputs(IOModel):
                pass

            class Outputs(Passthrough):
                pass

        assert serialize_output_schema(Collect) == {"_passthrough": True}

    def test_json_serializable(self) -> None:
        json.dumps(serialize_output_schema(_SchemaTool))

    def test_template_default_on_non_path_output_raises(self) -> None:
        class BadTemplateOutput(ProcessingTool):
            environment = _ENV

            class Inputs(IOModel):
                input_image: Path

            class Outputs(IOModel):
                status: str = Template("{input_image.stem}.txt")  # type: ignore[assignment]

            def process_row(self, arguments, *, context: object | None = None):
                return self.Outputs(status="ok")

        with pytest.raises(TypeError, match="Template default.*path output"):
            serialize_output_schema(BadTemplateOutput)

    def test_string_output_default_raises(self) -> None:
        class BadStringTemplateOutput(ProcessingTool):
            environment = _ENV

            class Inputs(IOModel):
                input_image: Path

            class Outputs(IOModel):
                mask: Path = "mask.tif"  # type: ignore[assignment]

            def process_row(self, arguments, *, context: object | None = None):
                return self.Outputs(mask=Path("mask.tif"))

        with pytest.raises(TypeError, match="must be declared with Template"):
            serialize_output_schema(BadStringTemplateOutput)

    def test_path_output_default_raises(self) -> None:
        class BadPathTemplateOutput(ProcessingTool):
            environment = _ENV

            class Inputs(IOModel):
                input_image: Path

            class Outputs(IOModel):
                mask: Path = Path("mask.tif")

            def process_row(self, arguments, *, context: object | None = None):
                return self.Outputs(mask=Path("mask.tif"))

        with pytest.raises(TypeError, match="must be declared with Template"):
            serialize_output_schema(BadPathTemplateOutput)

    def test_static_template_output_default_is_valid(self) -> None:
        class StaticTemplateOutput(ProcessingTool):
            environment = _ENV

            class Inputs(IOModel):
                input_image: Path

            class Outputs(IOModel):
                mask: Path = Template("mask.tif")

            def process_row(self, arguments, *, context: object | None = None):
                return self.Outputs(mask=Path("mask.tif"))

        schema = serialize_output_schema(StaticTemplateOutput)
        assert schema["mask"]["default"] == "mask.tif"
        assert schema["mask"]["template"] == "mask.tif"

    def test_image_path_gui_meta_preserved(self) -> None:
        schema = serialize_output_schema(_ImageFieldGuiTool)
        mask = schema["mask"]
        assert mask["type"] == "ImageFile"
        assert mask["display_name"] == "Segmentation mask"
        assert mask["description"] == "A label image."
        assert mask["group"] == "results"
        assert mask["image_spec"] == {
            "semantics": ["label"],
            "layouts": [],
            "dtypes": [],
            "formats": [".tif"],
        }


@pytest.mark.parametrize(
    "tool_cls", _all_common_tool_classes(), ids=lambda c: c.__name__
)
def test_common_tool_serializes_to_json(tool_cls: type) -> None:
    inputs = serialize_input_schema(tool_cls)
    outputs = serialize_output_schema(tool_cls)
    json.dumps(inputs)
    json.dumps(outputs)
    # Every declared input field is present.
    declared_inputs = set(tool_cls.Inputs._get_all_annotations())
    assert set(inputs.keys()) == declared_inputs


def test_common_tool_image_fields_use_imagefile_without_converting_plain_paths() -> (
    None
):
    from bioimageflow_common_tools import Files, LabelOverlaps, Mosaic
    from bioimageflow_common_tools.connected_components import ConnectedComponents

    mosaic_inputs = serialize_input_schema(Mosaic)
    mosaic_outputs = serialize_output_schema(Mosaic)
    assert mosaic_inputs["input_image"]["type"] == "ImageFile"
    assert mosaic_outputs["mosaic_path"]["type"] == "ImageFile"

    files_outputs = serialize_output_schema(Files)
    assert files_outputs["path"]["type"] == "Path"

    connected_components_inputs = serialize_input_schema(ConnectedComponents)
    connected_components_outputs = serialize_output_schema(ConnectedComponents)
    assert connected_components_inputs["input_image"]["type"] == "ImageFile"
    assert connected_components_outputs["output_image"]["type"] == "ImageFile"

    label_inputs = serialize_input_schema(LabelOverlaps)
    label_outputs = serialize_output_schema(LabelOverlaps)
    assert label_inputs["label_image"]["type"] == "ImageFile"
    assert label_inputs["reference_image"]["type"] == "ImageFile"
    assert label_outputs["reference_label"]["type"] == "int"
    assert label_outputs["spot_label"]["type"] == "int"
    assert label_outputs["overlap_count"]["type"] == "int"


def test_schema_serialization_error_exists() -> None:
    assert issubclass(SchemaSerializationError, Exception)
