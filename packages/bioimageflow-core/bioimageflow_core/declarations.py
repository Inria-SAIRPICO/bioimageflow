"""One portable semantic IO declaration for controller and worker admission."""

from __future__ import annotations

from copy import deepcopy
from enum import Enum
import hashlib
import json
from typing import Any, Annotated, Union, get_args, get_origin
import types

from .tool import IOModel
from .type_descriptors import decode_annotation, encode_annotation
from .types import ImageSpec, annotation_metadata, extract_gui_meta

DECLARATION_CONTRACT_VERSION = "bioimageflow.tool_declaration.v1"
_CONSTRAINTS = {"min", "max", "gt", "ge", "lt", "le", "multiple_of", "min_length", "max_length"}
_FIELD_KEYS = {"type_spec", "required", "nullable", "constraints", "image_spec"}
_IMAGE_KEYS = {"semantics", "layouts", "dtypes", "formats"}


def _nullable(annotation: Any) -> bool:
    if get_origin(annotation) is Annotated:
        return _nullable(get_args(annotation)[0])
    origin = get_origin(annotation)
    return (origin is Union or origin is getattr(types, "UnionType", Union)) and type(None) in get_args(annotation)


def _constraints(annotation: Any) -> dict[str, Any]:
    facts: dict[str, Any] = {}
    gui = extract_gui_meta(annotation)
    if gui is not None:
        facts.update({name: getattr(gui, name) for name in ("min", "max") if getattr(gui, name) is not None})
    for item in annotation_metadata(annotation):
        # These finite metadata records can be inspected without installing or
        # importing Pydantic in a worker that does not use it.
        records = item.metadata if type(item).__module__ == "pydantic.fields" else (item,)
        for constraint in records:
            if type(constraint).__module__ == "annotated_types":
                for name in _CONSTRAINTS - {"min", "max"}:
                    if hasattr(constraint, name):
                        facts[name] = getattr(constraint, name)
    return facts


def _image(annotation: Any) -> Any:
    spec = next((item for item in annotation_metadata(annotation) if isinstance(item, ImageSpec)), None)
    if spec is None:
        return None
    result = {}
    for name in _IMAGE_KEYS:
        values = [item.value if isinstance(item, Enum) else item for item in getattr(spec, name)]
        if any(type(item) is not str for item in values):
            raise ValueError(f"image_spec.{name} requires string values")
        result[name] = sorted(values)
    return result


def describe_io_model(model: Any, *, passthrough: bool = False) -> Any:
    """Project semantics without copying defaults, viewer hints or local owners.

    None is an explicit absent/dynamic declaration for controller-only tools.
    Concrete worker tasks admit only non-null IOModel projections.
    """
    if model is None:
        return None
    if not isinstance(model, type) or not issubclass(model, IOModel):
        raise TypeError("An IO declaration must be an IOModel class")
    annotations = model._get_all_annotations()
    return _validate_model({
        "passthrough": passthrough,
        "field_names": list(annotations),
        "fields": {
            name: {"type_spec": encode_annotation(annotation),
                   "required": not hasattr(model, name), "nullable": _nullable(annotation),
                   "constraints": _constraints(annotation), "image_spec": _image(annotation)}
            for name, annotation in annotations.items()
        },
    }, "model")


def describe_tool_declaration(tool: Any) -> dict[str, Any]:
    """Describe a concrete processing tool's actual instance IO declarations."""
    return validate_tool_declaration({
        "inputs": describe_io_model(tool.Inputs),
        "outputs": describe_io_model(tool.Outputs),
    })


def _exact(value: Any, keys: set[str], path: str) -> dict[str, Any]:
    if type(value) is not dict or set(value) != keys:
        raise ValueError(f"{path} requires exactly {sorted(keys)}")
    return value


def _validate_model(value: Any, path: str) -> dict[str, Any]:
    model = _exact(value, {"passthrough", "field_names", "fields"}, path)
    if type(model["passthrough"]) is not bool:
        raise ValueError(f"{path}.passthrough must be boolean")
    names, fields = model["field_names"], model["fields"]
    if (type(names) is not list or any(type(name) is not str or not name for name in names)
            or len(set(names)) != len(names) or type(fields) is not dict or set(fields) != set(names)):
        raise ValueError(f"{path}.field_names must identify every field exactly once")
    for name in names:
        field_path = f"{path}.fields.{name}"
        field = _exact(fields[name], _FIELD_KEYS, field_path)
        try:
            decode_annotation(field["type_spec"])
        except (ValueError, TypeError) as exc:
            raise ValueError(f"{field_path}.type_spec: {exc}") from exc
        if type(field["required"]) is not bool or type(field["nullable"]) is not bool:
            raise ValueError(f"{field_path}.required/nullable must be boolean")
        constraints = field["constraints"]
        if (type(constraints) is not dict or not set(constraints) <= _CONSTRAINTS
                or any(type(item) not in (int, float) for item in constraints.values())):
            raise ValueError(f"{field_path}.constraints requires supported numeric bounds")
        image = field["image_spec"]
        if image is not None:
            _exact(image, _IMAGE_KEYS, f"{field_path}.image_spec")
            for key, items in image.items():
                if (type(items) is not list or any(type(item) is not str for item in items)
                        or items != sorted(set(items))):
                    raise ValueError(f"{field_path}.image_spec.{key} must be sorted unique strings")
    return deepcopy(model)


def validate_tool_declaration(value: Any) -> dict[str, Any]:
    """Pure exact admission of the full current processing-tool declaration."""
    declaration = _exact(value, {"inputs", "outputs"}, "declaration")
    result = {name: _validate_model(declaration[name], name) for name in ("inputs", "outputs")}
    if any(model["passthrough"] for model in result.values()):
        raise ValueError("Processing declarations cannot use controller-only Passthrough")
    return result


def _typed(value: Any) -> Any:
    # Explicit tags prevent Python equality from merging True/1/1.0, signed
    # zero or literal members. Dict order is immaterial; field_names is not.
    if value is None:
        return ["none"]
    if type(value) is bool:
        return ["bool", value]
    if type(value) is int:
        return ["int", str(value)]
    if type(value) is float:
        return ["float", value.hex()]
    if type(value) is str:
        return ["str", value]
    if type(value) is list:
        return ["list", [_typed(item) for item in value]]
    if type(value) is dict:
        return ["dict", [[key, _typed(value[key])] for key in sorted(value)]]
    raise ValueError("Unsupported declaration value")


def declaration_digest(value: Any) -> str:
    """Hash admitted typed semantic facts without Python repr or namespaces."""
    return _digest_admitted(validate_tool_declaration(value))


def _digest_admitted(value: Any) -> str:
    canonical = json.dumps(_typed(value), ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("ascii")).hexdigest()


def _difference(expected: Any, actual: Any, path: str) -> str:
    if type(expected) is not type(actual):
        return path
    if type(expected) is dict and set(expected) == set(actual):
        for key in expected:
            if _typed(expected[key]) != _typed(actual[key]):
                return _difference(expected[key], actual[key], f"{path}.{key}" if path else key)
    elif type(expected) is list and len(expected) == len(actual):
        for index, (left, right) in enumerate(zip(expected, actual)):
            if _typed(left) != _typed(right):
                return _difference(left, right, f"{path}[{index}]")
    return path


def compare_tool_declarations(expected: Any, actual: Any) -> None:
    """Refuse a mismatch with a precise path before scientific execution."""
    left, right = validate_tool_declaration(expected), validate_tool_declaration(actual)
    if _digest_admitted(left) != _digest_admitted(right):
        raise ValueError(f"Tool declaration mismatch at {_difference(left, right, '')}")
