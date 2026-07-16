"""Unit tests for the wire-format serializers in ``bioimageflow.validation``.

Covers ``_jsonify_default``, ``_display_type_name``, ``_extract_choices``,
``_serialize_connectable``, ``serialize_input_schema``, and
``serialize_output_schema`` per `plan-serialize-input-schema.md`.
"""

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


# ---------------------------------------------------------------------------
# _jsonify_default
# ---------------------------------------------------------------------------


class _Mode(str, Enum):
    FAST = "fast"
    ACCURATE = "accurate"


class _CustomDefault:
    def __str__(self) -> str:
        return "custom-obj"


class TestJsonifyDefault:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            pytest.param(None, None, id="none"),
            pytest.param(True, True, id="bool"),
            pytest.param(42, 42, id="int"),
            pytest.param(1.5, 1.5, id="float"),
            pytest.param("hello", "hello", id="str"),
            pytest.param(Path("a/b.txt"), str(Path("a/b.txt")), id="path"),
            pytest.param(_Mode.FAST, "fast", id="enum"),
            pytest.param([1, "two", Path("p")], [1, "two", str(Path("p"))], id="list"),
            pytest.param(("a", "b"), ["a", "b"], id="tuple"),
            pytest.param({"k": Path("p")}, {"k": str(Path("p"))}, id="dict"),
            pytest.param(
                [(1, 2), {"x": Path("a")}],
                [[1, 2], {"x": str(Path("a"))}],
                id="nested",
            ),
            pytest.param(_CustomDefault(), "custom-obj", id="fallback"),
        ],
    )
    def test_jsonify_default(self, value: Any, expected: Any) -> None:
        assert _jsonify_default(value) == expected


# ---------------------------------------------------------------------------
# _display_type_name
# ---------------------------------------------------------------------------


class TestDisplayTypeName:
    @pytest.mark.parametrize(
        ("annotation", "expected"),
        [
            pytest.param(int, "int", id="int"),
            pytest.param(float, "float", id="float"),
            pytest.param(str, "str", id="str"),
            pytest.param(bool, "bool", id="bool"),
            pytest.param(Path, "Path", id="path"),
            pytest.param(list[int], "list", id="list"),
            pytest.param(dict[str, int], "dict", id="dict"),
            pytest.param(tuple[str, str], "tuple", id="tuple"),
            pytest.param(Literal["a", "b"], "str", id="literal-str"),
            pytest.param(Literal[1, 2], "int", id="literal-int"),
            pytest.param(_Mode, "str", id="enum"),
            pytest.param(Annotated[int, GUIMeta(min=0)], "int", id="annotated"),
            pytest.param(int | None, "int", id="optional"),
            pytest.param(Optional[Annotated[int, GUIMeta(min=0)]], "int", id="optional-annotated"),
            pytest.param(Annotated[Path, ImageSpec()], "ImageFile", id="image-path"),
            pytest.param(ImageShared(), "ImageShared", id="image-shared"),
            pytest.param(Annotated[Path, GUIMeta(display_name="p")], "Path", id="plain-path"),
        ],
    )
    def test_display_type_name(self, annotation: Any, expected: str) -> None:
        assert _display_type_name(annotation) == expected


# ---------------------------------------------------------------------------
# _extract_choices
# ---------------------------------------------------------------------------


class TestExtractChoices:
    @pytest.mark.parametrize(
        ("annotation", "expected"),
        [
            pytest.param(int, None, id="plain-type"),
            pytest.param(Literal["a", "b", "c"], ["a", "b", "c"], id="literal-str"),
            pytest.param(Literal[1, 2, 3], ["1", "2", "3"], id="literal-int"),
            pytest.param(_Mode, ["fast", "accurate"], id="enum"),
            pytest.param(Annotated[Literal["a", "b"], GUIMeta()], ["a", "b"], id="annotated"),
            pytest.param(Literal["a", "b"] | None, ["a", "b"], id="optional"),
        ],
    )
    def test_extract_choices(self, annotation: Any, expected: list[str] | None) -> None:
        assert _extract_choices(annotation) == expected


# ---------------------------------------------------------------------------
# _is_nullable
# ---------------------------------------------------------------------------


class TestIsNullable:
    @pytest.mark.parametrize(
        ("annotation", "expected"),
        [
            pytest.param(int, False, id="bare-int"),
            pytest.param(int | None, True, id="int-or-none"),
            pytest.param(Optional[int], True, id="optional-int"),
            pytest.param(Annotated[int | None, GUIMeta(min=0)], True, id="annotated-optional"),
            pytest.param(Annotated[int, GUIMeta(min=0)], False, id="annotated-non-optional"),
            pytest.param(
                Annotated[Path, ImageSpec(semantics={Semantic.INTENSITY})],
                False,
                id="image-path",
            ),
            pytest.param(int | str | None, True, id="three-way-union"),
            pytest.param(Literal["a", "b"], False, id="literal"),
            pytest.param(Optional[Literal["a", "b"]], True, id="optional-literal"),
        ],
    )
    def test_is_nullable(self, annotation: Any, expected: bool) -> None:
        assert _is_nullable(annotation) is expected


# ---------------------------------------------------------------------------
# _serialize_connectable
# ---------------------------------------------------------------------------


class TestSerializeConnectable:
    @pytest.mark.parametrize(
        ("connectable", "expected"),
        [
            pytest.param(Connectable.NEVER, "never", id="never"),
            pytest.param(Connectable.NOT_BY_DEFAULT, "not_by_default", id="not-by-default"),
            pytest.param(Connectable.BY_DEFAULT, "by_default", id="by-default"),
            pytest.param(None, "not_by_default", id="none-default"),
        ],
    )
    def test_serialize_connectable(
        self,
        connectable: Connectable | None,
        expected: str,
    ) -> None:
        assert _serialize_connectable(connectable) == expected


# ---------------------------------------------------------------------------
# serialize_input_schema
# ---------------------------------------------------------------------------


_ENV = EnvironmentSpec(name="test-schema", dependencies={})


class _SchemaTool(ProcessingTool):
    display_name = "Schema Test Tool"
    category = Category.UTILITIES
    environment = _ENV

    class Inputs(IOModel):
        input_image: Annotated[
            Path,
            ImageSpec(semantics={Semantic.INTENSITY}, layouts={Layout.PLANAR}),
            GUIMeta(
                display_name="Input image",
                description="A 2D intensity image.",
                connectable=Connectable.BY_DEFAULT,
            ),
        ]
        diameter: Annotated[float, GUIMeta(min=0.0, max=100.0, step=0.1)] = 1.0
        model: Annotated[Literal["fast", "accurate"], GUIMeta(group="advanced")] = "fast"
        mode_enum: _Mode = _Mode.FAST
        count: int = 5
        # Nullable with default None — e.g. a CLI flag that should be omitted
        # entirely when the user wants the binary's built-in default.
        area_lim: float | None = None
        # Nullable but required — user must explicitly pass a value or None.
        threshold: int | None

    class Outputs(IOModel):
        mask: Annotated[
            Path,
            ImageSpec(semantics={Semantic.LABEL}),
            GUIMeta(display_name="Mask"),
        ] = Template("{input_image.stem}_mask{ext}")
        cell_count: int

    def process_row(self, arguments, *, context: object | None = None):  # type: ignore[override]
        return self.Outputs(mask=Path("x"), cell_count=0)


class _ImageFieldGuiTool(ProcessingTool):
    display_name = "Image Field GUI Test Tool"
    category = Category.UTILITIES
    environment = _ENV

    class Inputs(IOModel):
        input_image: Annotated[
            Path,
            ImageSpec(
                semantics={Semantic.INTENSITY},
                layouts={Layout.PLANAR},
            ),
            GUIMeta(
                display_name="Input image",
                description="A 2D intensity image.",
                connectable=Connectable.BY_DEFAULT,
                group="data",
            ),
        ]

    class Outputs(IOModel):
        mask: Annotated[
            Path,
            ImageSpec(
                semantics={Semantic.LABEL},
                formats={".tif"},
            ),
            GUIMeta(
                display_name="Segmentation mask",
                description="A label image.",
                group="results",
            ),
        ] = Template("{input_image.stem}_mask{ext}")

    def process_row(self, arguments, *, context: object | None = None):  # type: ignore[override]
        return self.Outputs(mask=Path("x"))


class TestSerializeInputSchema:
    def test_keys_present(self) -> None:
        schema = serialize_input_schema(_SchemaTool)
        expected_keys = {
            "type", "required", "nullable", "connectable", "default",
            "display_name", "description", "group",
            "min", "max", "step", "path_picker", "choices", "image_spec",
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


# ---------------------------------------------------------------------------
# serialize_output_schema
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Integration: all common tools must produce JSON-serializable schemas
# ---------------------------------------------------------------------------


def _all_common_tool_classes() -> list[type]:
    import bioimageflow_common_tools as ct

    classes = []
    for name in dir(ct):
        obj = getattr(ct, name)
        if isinstance(obj, type) and hasattr(obj, "Inputs"):
            classes.append(obj)
    return classes


@pytest.mark.parametrize("tool_cls", _all_common_tool_classes(), ids=lambda c: c.__name__)
def test_common_tool_serializes_to_json(tool_cls: type) -> None:
    inputs = serialize_input_schema(tool_cls)
    outputs = serialize_output_schema(tool_cls)
    json.dumps(inputs)
    json.dumps(outputs)
    # Every declared input field is present.
    declared_inputs = set(tool_cls.Inputs._get_all_annotations())
    assert set(inputs.keys()) == declared_inputs


def test_common_tool_image_fields_use_imagefile_without_converting_plain_paths() -> None:
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


# ---------------------------------------------------------------------------
# Snapshot: lock the wire format against a representative real tool
# ---------------------------------------------------------------------------


FIXTURES_DIR = Path(__file__).parent / "fixtures"


def _canonical(value):  # type: ignore[no-untyped-def]
    """Sort every list inside the schema so snapshot comparison is stable."""
    if isinstance(value, dict):
        return {k: _canonical(v) for k, v in value.items()}
    if isinstance(value, list):
        return sorted((_canonical(v) for v in value), key=lambda x: json.dumps(x, sort_keys=True))
    return value


# ---------------------------------------------------------------------------
# serialize_tool_metadata
# ---------------------------------------------------------------------------


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
            Collect, Concat, CrossJoin, InnerJoin, JoinOnColumn,
        )

        for tool_cls in (InnerJoin, CrossJoin, JoinOnColumn, Concat, Collect):
            assert serialize_tool_metadata(tool_cls)["dynamic_outputs"] is True, (
                f"{tool_cls.__name__} should report dynamic_outputs=True "
                f"because it overrides resolve_merge_schema"
            )


# ---------------------------------------------------------------------------
# serialize_resolved_outputs
# ---------------------------------------------------------------------------


class TestSerializeResolvedOutputs:
    def test_unresolved_when_inputs_missing(self) -> None:
        from bioimageflow import Workflow
        from bioimageflow.dataframe_tool import DataFrameTool

        class Unconfigured(DataFrameTool):
            display_name = "Unconfigured"

            class Inputs(IOModel):
                pass

            @classmethod
            def resolve_outputs(cls, inputs=None):
                return None

        with Workflow(engine="direct"):
            n = Unconfigured()()
            out = serialize_resolved_outputs(n)
            assert out == {"resolved": False, "columns": {}}

    def test_resolved_after_inputs_set(self) -> None:
        from bioimageflow import Workflow
        from bioimageflow_common_tools import Generate

        with Workflow(engine="direct"):
            g = Generate()(column_name="sensitivity", values=[1, 2])
            out = serialize_resolved_outputs(g)
            assert out["resolved"] is True
            assert "sensitivity" in out["columns"]

    def test_json_safe(self) -> None:
        from bioimageflow import Workflow
        from bioimageflow_common_tools import Generate

        with Workflow(engine="direct"):
            g = Generate()(column_name="x", values=[1])
            json.dumps(serialize_resolved_outputs(g))


def test_snapshot_connected_components() -> None:
    from bioimageflow_common_tools.connected_components import ConnectedComponents

    actual = {
        "inputs": serialize_input_schema(ConnectedComponents),
        "outputs": serialize_output_schema(ConnectedComponents),
    }
    expected = json.loads((FIXTURES_DIR / "connected_components_schema.json").read_text())
    assert _canonical(actual) == _canonical(expected)
