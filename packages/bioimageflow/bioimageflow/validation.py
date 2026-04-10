"""Orchestrator-side validation helpers."""

import hashlib
import importlib.metadata
import inspect
import os
from typing import Annotated, Any, get_args, get_origin

from bioimageflow_core.types import Connectable, ImageSpec, extract_gui_meta
from bioimageflow_core.tool import IOModel, BaseTool


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


def get_inputs_schema(tool: BaseTool) -> dict[str, dict[str, Any]]:
    """Return a GUI-friendly schema for all input fields of a tool.

    For each field in the tool's ``Inputs``, the returned dict contains:

    - **type**: the base Python type (e.g. ``float``, ``Path``)
    - **default**: the default value, or ``None`` if required
    - **required**: whether the field has no default
    - **connectable**: whether this field accepts an upstream column binding
    - **image_spec**: the :class:`~bioimageflow_core.ImageSpec` if present
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
            "connectable": gui_meta.connectable if gui_meta else Connectable.BY_DEFAULT,
            "image_spec": image_spec,
        }

        if gui_meta is not None:
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
