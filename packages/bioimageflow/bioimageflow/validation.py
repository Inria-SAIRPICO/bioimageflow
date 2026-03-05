"""Orchestrator-side validation helpers."""

import hashlib
import importlib.metadata
import inspect
import os
from typing import Annotated, Any, get_args, get_origin

from bioimageflow_core.types import ImageSpec
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


def get_tool_version(tool: BaseTool) -> str:
    """Extract version of the Python package containing the tool class."""
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
