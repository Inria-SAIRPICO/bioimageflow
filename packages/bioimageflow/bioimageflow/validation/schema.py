"""Focused orchestrator validation behavior."""

from __future__ import annotations

from .common import (
    Annotated,
    Any,
    BaseModel,
    BaseTool,
    Connectable,
    IOModel,
    ImageSpec,
    Path,
    Union,
    UnionType,
    create_model,
    extract_gui_meta,
    get_args,
    get_origin,
    hashlib,
    importlib,
    inspect,
    os,
)
from .models import (
    ValidationError,
)


def build_pydantic_model(tool_model_cls: type[IOModel]) -> type[BaseModel]:
    """Convert an IOModel declaration into a Pydantic model for validation."""
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
    """Check if an annotation is Path-based (Path or Annotated[Path, ...])."""
    from pathlib import Path

    inner = _unwrap_optional(annotation)
    if inner is Path:
        return True
    origin = get_origin(inner)
    if origin is Union or origin is UnionType:
        return Path in get_args(inner)
    if get_origin(inner) is Annotated:
        base = _unwrap_optional(get_args(inner)[0])
        if base is Path:
            return True
        origin = get_origin(base)
        if origin is Union or origin is UnionType:
            return Path in get_args(base)
    return False


def is_image_type(annotation: Any) -> bool:
    """Check if annotation is an image field (Annotated with ImageSpec)."""
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
    - **path_picker**: file/folder picker mode from :class:`~bioimageflow_core.GUIMeta`
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
            "connectable": gui_meta.connectable
            if gui_meta
            else Connectable.NOT_BY_DEFAULT,
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
            if gui_meta.path_picker is not None:
                entry["path_picker"] = gui_meta.path_picker

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
            errors.append(
                ValidationError(
                    kind="parameter_invalid",
                    message=err.get("msg", "invalid parameter"),
                    node=node,
                    field=field_name,
                )
            )
    return errors


def get_tool_version(tool: BaseTool) -> str:
    """Extract version of the Python package containing the tool class.

    Checks ``_bif_package_version`` first (set by the versioned tool loader),
    then embedded custom-tool source hashes, then falls back to
    ``importlib.metadata``, then source-file content hash, and finally
    file mtime.
    """
    tool_class = type(tool)
    bif_version = getattr(tool_class, "_bif_package_version", None)
    if bif_version is not None:
        return bif_version
    try:
        from bioimageflow.workflow import _get_custom_tools_dir_bundle_hash

        bundle_hash = _get_custom_tools_dir_bundle_hash(tool_class)
        if bundle_hash is not None:
            return f"source:{bundle_hash}"
    except Exception:
        pass
    source_hash = getattr(tool_class, "_bif_custom_source_hash", None)
    if source_hash is not None:
        return f"source:{source_hash}"
    try:
        module = tool.__module__
        package = module.split(".")[0]
        return importlib.metadata.version(package)
    except Exception:
        pass
    try:
        source_file = Path(inspect.getfile(tool.__class__))
        return "source:" + hashlib.sha256(source_file.read_bytes()).hexdigest()
    except Exception:
        pass
    try:
        source_file = inspect.getfile(tool_class)
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


def _unwrap_optional(annotation: Any) -> Any:
    """If ``annotation`` is ``Optional[X]`` or ``X | None``, return ``X``; otherwise pass through.

    Only unwraps when exactly one non-``None`` argument remains.
    """
    origin = get_origin(annotation)
    if origin is Union or origin is UnionType:
        args = [a for a in get_args(annotation) if a is not type(None)]
        if len(args) == 1:
            return args[0]
    return annotation
