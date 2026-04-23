"""Orchestrator-side validation helpers."""

import hashlib
import importlib.metadata
import inspect
import os
from dataclasses import dataclass
from typing import Annotated, Any, Literal, get_args, get_origin

from bioimageflow_core.types import Connectable, ImageSpec, extract_gui_meta
from bioimageflow_core.tool import IOModel, BaseTool


ValidationErrorKind = Literal[
    "cycle",
    "type_mismatch",
    "missing_input",
    "unknown_input",
    "column_not_found",
    "parameter_invalid",
    "unknown_tool",
    "duplicate_name",
    "construction_failed",
]


@dataclass(frozen=True)
class ValidationError:
    """A single problem found during graph construction or validation.

    Instances are produced by the error-collector (:meth:`Workflow.collect_errors`),
    ``Workflow.from_dict(..., collect_errors=True)``, and
    ``Workflow.validate()``. Consumers (GUIs, linters) map these to their own
    display formats. The library never raises ``ValidationError``; it raises
    the existing exceptions unless an error-collector is active.
    """
    kind: ValidationErrorKind
    message: str
    node: str | None = None
    field: str | None = None
    edge: tuple[str, str, str] | None = None
    path: tuple[str, ...] = ()


def build_pydantic_model(tool_model_cls: type[IOModel]) -> Any:
    """Convert an IOModel declaration into a Pydantic model for validation."""
    from pydantic import create_model

    fields: dict[str, Any] = {}
    for name, annotation in tool_model_cls._get_all_annotations().items():
        default = getattr(tool_model_cls, name, ...)
        fields[name] = (annotation, default)
    return create_model(tool_model_cls.__name__, **fields)


def extract_image_spec(annotation: Any) -> ImageSpec | None:
    """Extract ImageSpec from an Annotated type, or return None."""
    if get_origin(annotation) is Annotated:
        for arg in get_args(annotation):
            if isinstance(arg, ImageSpec):
                return arg
    return None


def is_path_type(annotation: Any) -> bool:
    """Check if an annotation is Path-based (Path, ImagePath, Annotated[Path, ...])."""
    from pathlib import Path

    if annotation is Path:
        return True
    if get_origin(annotation) is Annotated:
        base = get_args(annotation)[0]
        return base is Path
    return False


def is_image_type(annotation: Any) -> bool:
    """Check if annotation is ImagePath or ImageShared (Annotated with ImageSpec)."""
    return extract_image_spec(annotation) is not None


def serialize_image_spec(spec: ImageSpec | None) -> dict[str, list[str]] | None:
    """Return a JSON-friendly representation of an :class:`ImageSpec`.

    Shape: ``{"semantics": [...], "layouts": [...], "dtypes": [...], "formats": [...]}``
    where each list contains enum value strings. Returns ``None`` when
    ``spec`` is ``None``. Used by GUIs that expose type information in
    their widgets.
    """
    if spec is None:
        return None

    def _as_str(v: Any) -> str:
        # Enum → value, otherwise str().
        val = getattr(v, "value", v)
        return str(val)

    return {
        "semantics": sorted(_as_str(s) for s in spec.semantics),
        "layouts": sorted(_as_str(layout) for layout in spec.layouts),
        "dtypes": sorted(_as_str(d) for d in spec.dtypes),
        "formats": sorted(_as_str(f) for f in spec.formats),
    }


def get_inputs_schema(tool: BaseTool) -> dict[str, dict[str, Any]]:
    """Return a GUI-friendly schema for all input fields of a tool.

    For each field in the tool's ``Inputs``, the returned dict contains:

    - **type**: the base Python type (e.g. ``float``, ``Path``)
    - **default**: the default value, or ``None`` if required
    - **required**: whether the field has no default
    - **connectable**: whether this field accepts an upstream column binding
    - **image_spec**: the :class:`~bioimageflow_core.ImageSpec` if present
    - **display_name**, **description**: human-readable label / tooltip from
      :class:`~bioimageflow_core.GUIMeta`
    - **min**, **max**, **step**: numeric constraints from :class:`~bioimageflow_core.GUIMeta`
    - **group**: tab/section group name from :class:`~bioimageflow_core.GUIMeta`
    """
    inputs_cls = tool.Inputs
    annotations = inputs_cls._get_all_annotations()
    schema: dict[str, dict[str, Any]] = {}

    for field_name, annotation in annotations.items():
        base_type = annotation
        image_spec = extract_image_spec(annotation)
        gui_meta = extract_gui_meta(annotation)

        if get_origin(annotation) is Annotated:
            base_type = get_args(annotation)[0]

        has_default = hasattr(inputs_cls, field_name)
        default = getattr(inputs_cls, field_name, None)

        entry: dict[str, Any] = {
            "type": base_type,
            "default": default,
            "required": not has_default,
            "connectable": gui_meta.connectable if gui_meta else Connectable.NOT_BY_DEFAULT,
            "image_spec": image_spec,
            "image_spec_serialized": serialize_image_spec(image_spec),
        }

        if gui_meta is not None:
            if gui_meta.display_name is not None:
                entry["display_name"] = gui_meta.display_name
            if gui_meta.description is not None:
                entry["description"] = gui_meta.description
            if gui_meta.min is not None:
                entry["min"] = gui_meta.min
            if gui_meta.max is not None:
                entry["max"] = gui_meta.max
            if gui_meta.step is not None:
                entry["step"] = gui_meta.step
            if gui_meta.group is not None:
                entry["group"] = gui_meta.group

        schema[field_name] = entry

    return schema


def check_type_compat(
    node: Any,
    field: str,
    col_ref: Any,
) -> ValidationError | None:
    """Return a ``ValidationError`` if ``col_ref`` is incompatible with ``node.<field>``.

    Pure function — does not raise; returns None on success. Used by
    :meth:`Workflow.validate`. Mirrors the (internal) logic in
    ``Node._check_type_compat`` but reports errors instead of raising.
    """
    from bioimageflow_core.types import check_compatibility

    input_annotations = node.tool.Inputs._get_all_annotations()
    consumer_spec = extract_image_spec(input_annotations.get(field))
    if consumer_spec is None:
        return None

    upstream_outputs = col_ref.node.tool.Outputs
    if upstream_outputs is None:
        return None

    output_annotations = upstream_outputs._get_all_annotations()
    if col_ref.column not in output_annotations:
        return None  # reported elsewhere (column_not_found)

    producer_spec = extract_image_spec(output_annotations[col_ref.column])
    if producer_spec is None:
        return None

    if not check_compatibility(producer_spec, consumer_spec):
        return ValidationError(
            kind="type_mismatch",
            message=(
                f"Type mismatch: upstream '{col_ref.node.name}'.'{col_ref.column}' "
                f"is not compatible with input '{field}' of tool "
                f"'{type(node.tool).__name__}'. "
                f"Producer semantics: {producer_spec.semantics}, "
                f"consumer semantics: {consumer_spec.semantics}."
            ),
            node=node.name,
            field=field,
            edge=(col_ref.node.name, node.name, field),
        )
    return None


def validate_parameters(
    tool_class: type,
    parameters: dict[str, Any],
    *,
    node: str | None = None,
) -> list[ValidationError]:
    """Validate a dict of constants against a tool class's ``Inputs``.

    Only the supplied parameters are checked — missing required fields
    are not reported here (that is ``missing_input`` in
    :meth:`Workflow.validate`). Each Pydantic error is mapped to a
    ``parameter_invalid`` entry.
    """
    import pydantic

    inputs_cls = tool_class.Inputs  # type: ignore[attr-defined]
    annotations = inputs_cls._get_all_annotations()
    # Only validate fields that are declared on Inputs AND supplied.
    known = {k: v for k, v in parameters.items() if k in annotations}
    if not known:
        return []

    # Build a pydantic model containing just the supplied fields, keeping
    # their original annotations. This lets us surface range/type errors
    # without raising on fields the caller didn't supply.
    from pydantic import create_model

    fields: dict[str, Any] = {name: (annotations[name], ...) for name in known}
    model = create_model(f"{tool_class.__name__}_Params", **fields)

    errors: list[ValidationError] = []
    try:
        model(**known)
    except pydantic.ValidationError as exc:
        for err in exc.errors():
            loc = err.get("loc", ())
            field_name = str(loc[0]) if loc else None
            errors.append(ValidationError(
                kind="parameter_invalid",
                message=err.get("msg", "invalid parameter"),
                node=node,
                field=field_name,
            ))
    return errors


def get_tool_version(tool: BaseTool) -> str:
    """Extract version of the Python package containing the tool class.

    Checks ``_bif_package_version`` first (set by the versioned tool loader),
    then falls back to ``importlib.metadata`` and finally file mtime.
    """
    bif_version = getattr(type(tool), "_bif_package_version", None)
    if bif_version is not None:
        return bif_version
    try:
        module = tool.__module__
        package = module.split('.')[0]
        return importlib.metadata.version(package)
    except Exception:
        pass
    try:
        source_file = inspect.getfile(tool.__class__)
        return str(os.path.getmtime(source_file))
    except Exception:
        return "unversioned"


def get_source_hash(tool_class: type[Any]) -> str:
    """SHA256 of the tool class source code, for dev mode."""
    try:
        source = inspect.getsource(tool_class)
        return hashlib.sha256(source.encode()).hexdigest()
    except (OSError, TypeError):
        return "nosource"
