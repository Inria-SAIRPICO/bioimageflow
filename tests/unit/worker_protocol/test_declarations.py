"""Shared semantic declarations, independent of Python namespace identity."""

from copy import deepcopy
from typing import Annotated, Literal, Optional

import pytest

from bioimageflow_core import GUIMeta, IOModel, ImageSpec, Layout, Semantic
from bioimageflow_core.declarations import (
    compare_tool_declarations,
    declaration_digest,
    describe_io_model,
    validate_tool_declaration,
)


def _declaration(inputs, outputs):
    return {"inputs": describe_io_model(inputs), "outputs": describe_io_model(outputs)}


def test_independent_model_names_share_semantic_declaration():
    class ControllerInputs(IOModel):
        value: Optional[int]
        enabled: bool = True

    class WorkerInputs(IOModel):
        value: Optional[int]
        enabled: bool = False

    class ControllerOutputs(IOModel):
        value: int

    class WorkerOutputs(IOModel):
        value: int

    controller = _declaration(ControllerInputs, ControllerOutputs)
    worker = _declaration(WorkerInputs, WorkerOutputs)
    compare_tool_declarations(controller, worker)
    assert declaration_digest(controller) == declaration_digest(worker)
    assert controller["inputs"]["fields"]["value"]["required"] is True
    assert controller["inputs"]["fields"]["value"]["nullable"] is True
    assert "default" not in controller["inputs"]["fields"]["enabled"]


def test_field_order_is_declaration_authority():
    class First(IOModel):
        left: int
        right: int

    class Reordered(IOModel):
        right: int
        left: int

    with pytest.raises(ValueError, match="outputs.field_names"):
        compare_tool_declarations(_declaration(IOModel, First), _declaration(IOModel, Reordered))


@pytest.mark.parametrize("left,right", [(True, 1), (1, 1.0), (-0.0, 0.0)])
def test_equal_python_scalars_have_distinct_declaration_digests(left, right):
    class Boolean(IOModel):
        value: Literal[True]

    boolean = _declaration(IOModel, Boolean)
    integer = deepcopy(boolean)
    boolean["outputs"]["fields"]["value"]["type_spec"]["values"] = [left]
    integer["outputs"]["fields"]["value"]["type_spec"]["values"] = [right]
    assert declaration_digest(boolean) != declaration_digest(integer)
    with pytest.raises(ValueError, match="outputs.fields.value.type_spec"):
        compare_tool_declarations(boolean, integer)


def test_optional_metadata_orders_share_semantic_bounds_and_image():
    spec = ImageSpec(semantics={Semantic.INTENSITY}, layouts={Layout.PLANAR}, dtypes={"uint16"})

    class Outer(IOModel):
        image: Annotated[Optional[int], spec, GUIMeta(min=0, max=255)]

    class Inner(IOModel):
        image: Annotated[Optional[Annotated[int, GUIMeta(min=0, max=255)]], spec]

    compare_tool_declarations(_declaration(Outer, IOModel), _declaration(Inner, IOModel))
    field = describe_io_model(Outer)["fields"]["image"]
    assert field["nullable"] is True
    assert field["constraints"] == {"min": 0, "max": 255}
    assert field["image_spec"]["dtypes"] == ["uint16"]


def test_projection_detaches_metadata_and_never_serializes_default_values():
    sentinel = object()

    class Inputs(IOModel):
        value: int = sentinel

    descriptor = _declaration(Inputs, IOModel)
    admitted = validate_tool_declaration(descriptor)
    descriptor["inputs"]["field_names"].append("foreign")
    descriptor["inputs"]["fields"]["value"]["required"] = True
    assert admitted["inputs"]["field_names"] == ["value"]
    assert admitted["inputs"]["fields"]["value"]["required"] is False
    assert Inputs.value is sentinel


@pytest.mark.parametrize("mutation", [
    lambda value: value["outputs"].update(extra=True),
    lambda value: value["outputs"]["field_names"].append("missing"),
    lambda value: value["outputs"]["fields"]["value"].update(required=1),
    lambda value: value["outputs"]["fields"]["value"]["type_spec"].update(extra=True),
])
def test_malformed_declaration_fails_pure_admission(mutation):
    class Outputs(IOModel):
        value: int

    descriptor = _declaration(IOModel, Outputs)
    mutation(descriptor)
    with pytest.raises(ValueError):
        validate_tool_declaration(descriptor)
