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
    _CustomDefault,
    _Mode,
)


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
            pytest.param(
                Optional[Annotated[int, GUIMeta(min=0)]], "int", id="optional-annotated"
            ),
            pytest.param(Annotated[Path, ImageSpec()], "ImageFile", id="image-path"),
            pytest.param(ImageShared(), "ImageShared", id="image-shared"),
            pytest.param(
                Annotated[Path, GUIMeta(display_name="p")], "Path", id="plain-path"
            ),
        ],
    )
    def test_display_type_name(self, annotation: Any, expected: str) -> None:
        assert _display_type_name(annotation) == expected


class TestExtractChoices:
    @pytest.mark.parametrize(
        ("annotation", "expected"),
        [
            pytest.param(int, None, id="plain-type"),
            pytest.param(Literal["a", "b", "c"], ["a", "b", "c"], id="literal-str"),
            pytest.param(Literal[1, 2, 3], ["1", "2", "3"], id="literal-int"),
            pytest.param(_Mode, ["fast", "accurate"], id="enum"),
            pytest.param(
                Annotated[Literal["a", "b"], GUIMeta()], ["a", "b"], id="annotated"
            ),
            pytest.param(Literal["a", "b"] | None, ["a", "b"], id="optional"),
        ],
    )
    def test_extract_choices(self, annotation: Any, expected: list[str] | None) -> None:
        assert _extract_choices(annotation) == expected


class TestIsNullable:
    @pytest.mark.parametrize(
        ("annotation", "expected"),
        [
            pytest.param(int, False, id="bare-int"),
            pytest.param(int | None, True, id="int-or-none"),
            pytest.param(Optional[int], True, id="optional-int"),
            pytest.param(
                Annotated[int | None, GUIMeta(min=0)], True, id="annotated-optional"
            ),
            pytest.param(
                Annotated[int, GUIMeta(min=0)], False, id="annotated-non-optional"
            ),
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


class TestSerializeConnectable:
    @pytest.mark.parametrize(
        ("connectable", "expected"),
        [
            pytest.param(Connectable.NEVER, "never", id="never"),
            pytest.param(
                Connectable.NOT_BY_DEFAULT, "not_by_default", id="not-by-default"
            ),
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
