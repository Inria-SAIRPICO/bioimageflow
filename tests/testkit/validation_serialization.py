"""Shared helpers for the focused tests split from ``tests/unit/test_validation_serialization.py``."""

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


class _Mode(str, Enum):
    FAST = "fast"
    ACCURATE = "accurate"


class _CustomDefault:
    def __str__(self) -> str:
        return "custom-obj"


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
        model: Annotated[Literal["fast", "accurate"], GUIMeta(group="advanced")] = (
            "fast"
        )
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


def _all_common_tool_classes() -> list[type]:
    import bioimageflow_common_tools as ct

    classes = []
    for name in dir(ct):
        obj = getattr(ct, name)
        if isinstance(obj, type) and hasattr(obj, "Inputs"):
            classes.append(obj)
    return classes


FIXTURES_DIR = Path(__file__).parents[1] / "unit" / "fixtures"


def _canonical(value):  # type: ignore[no-untyped-def]
    """Sort every list inside the schema so snapshot comparison is stable."""
    if isinstance(value, dict):
        return {k: _canonical(v) for k, v in value.items()}
    if isinstance(value, list):
        return sorted(
            (_canonical(v) for v in value), key=lambda x: json.dumps(x, sort_keys=True)
        )
    return value
