"""Postponed tool declarations share resolved validation and GUI schemas."""

from __future__ import annotations

import sys
from pathlib import Path
from types import ModuleType
from typing import Annotated, get_args

import pytest

from bioimageflow import DataFrameTool
from bioimageflow.validation import (
    build_pydantic_model,
    get_inputs_schema,
    serialize_input_schema,
    serialize_output_schema,
    validate_parameters,
)
from bioimageflow_core import Connectable, GUIMeta, IOModel, ImageSpec


class BaseInputs(IOModel):
    column_name: Annotated[str, GUIMeta(connectable=Connectable.NEVER)] = "value"


class Generate(DataFrameTool):
    class Inputs(BaseInputs):
        count: int = 3

    class Outputs(IOModel):
        image: Annotated[Path, ImageSpec(semantics={"label"}), GUIMeta(display_name="Mask")]


def test_postponed_parameters_validate_and_keep_gui_metadata() -> None:
    assert validate_parameters(Generate, {"column_name": "number", "count": 2}) == []
    errors = validate_parameters(Generate, {"column_name": [], "count": "invalid"})
    assert {(error.kind, error.field) for error in errors} == {
        ("parameter_invalid", "column_name"),
        ("parameter_invalid", "count"),
    }
    model = build_pydantic_model(Generate.Inputs)(column_name="number", count=2)
    assert model.model_dump() == {"column_name": "number", "count": 2}

    schema = serialize_input_schema(Generate)
    assert schema["column_name"]["type"] == "str"
    assert schema["column_name"]["connectable"] == "never"
    assert schema["column_name"]["default"] == "value"
    assert schema["count"]["type"] == "int"
    assert get_inputs_schema(Generate())["column_name"]["type"] is str


def test_postponed_output_preserves_image_and_gui_annotations() -> None:
    annotation = Generate.Outputs._get_all_annotations()["image"]
    assert get_args(annotation)[0] is Path
    assert isinstance(get_args(annotation)[1], ImageSpec)
    assert isinstance(get_args(annotation)[2], GUIMeta)
    schema = serialize_output_schema(Generate)["image"]
    assert schema["type"] == "ImageFile"
    assert schema["image_spec"]["semantics"] == ["label"]
    assert schema["display_name"] == "Mask"


def test_inherited_fields_resolve_in_each_defining_module_and_class(monkeypatch) -> None:
    base_module = ModuleType("_annotation_test_base")
    child_module = ModuleType("_annotation_test_child")
    monkeypatch.setitem(sys.modules, base_module.__name__, base_module)
    monkeypatch.setitem(sys.modules, child_module.__name__, child_module)
    exec(
        "from __future__ import annotations\n"
        "from bioimageflow_core import IOModel\n"
        "Alias = int\n"
        "ClassAlias = str\n"
        "class Base(IOModel):\n"
        "    ClassAlias = bool\n"
        "    inherited: Alias\n"
        "    inherited_local: ClassAlias\n"
        "    overridden: int\n",
        base_module.__dict__,
    )
    exec(
        "from __future__ import annotations\n"
        "from _annotation_test_base import Base\n"
        "Alias = str\n"
        "ClassAlias = int\n"
        "class Child(Base):\n"
        "    ClassAlias = float\n"
        "    added: Alias\n"
        "    local: ClassAlias\n"
        "    overridden: str\n",
        child_module.__dict__,
    )

    assert child_module.Child._get_all_annotations() == {
        "inherited": int,
        "inherited_local": bool,
        "overridden": str,
        "added": str,
        "local": float,
    }


def test_unresolved_annotations_fail_instead_of_becoming_wire_types() -> None:
    class Invalid(DataFrameTool):
        class Inputs(IOModel):
            value: MissingAnnotationType  # noqa: F821

    with pytest.raises(NameError, match="MissingAnnotationType"):
        serialize_input_schema(Invalid)
    with pytest.raises(NameError, match="MissingAnnotationType"):
        validate_parameters(Invalid, {"value": 1})
