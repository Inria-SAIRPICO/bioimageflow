"""Orchestrator-side validation helpers."""

import hashlib
import importlib.metadata
import inspect
import os
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from types import UnionType
from typing import Annotated, Any, Literal, Union, get_args, get_origin

from bioimageflow_core.types import Connectable, ImageSpec, extract_gui_meta
from bioimageflow_core.tool import IOModel, BaseTool


class SchemaSerializationError(Exception):
    """Raised when :func:`serialize_input_schema` / :func:`serialize_output_schema`
    cannot produce a wire-format schema for a tool class — typically because
    the tool class could not be instantiated for introspection.
    """


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
    "source_tool_upstream",
]


@dataclass(frozen=True)
class ValidationError:
    """A single problem found during graph construction or validation.

    Instances are produced by :meth:`Workflow.capture_errors`,
    ``Workflow.from_dict(..., partial=True)``, and ``Workflow.validate()``.
    Consumers (GUIs, linters) map these to their own display formats. The
    library never raises ``ValidationError``; it raises the existing
    exceptions unless an error-collector is active.

    ``edge`` carries the structural ``(from_node, to_node, field)`` triple.
    ``edge_id`` is an optional opaque identifier that GUIs can attach to
    edges via the ``id`` key in the wire format; the library round-trips
    it through :meth:`Workflow.to_dict` / :meth:`Workflow.from_dict` and
    copies it onto every ``ValidationError`` raised against that edge.
    This is the disambiguator for cases like positional args, where
    multiple edges share the same triple by construction.
    """
    kind: ValidationErrorKind
    message: str
    node: str | None = None
    field: str | None = None
    edge: tuple[str, str, str] | None = None
    edge_id: str | None = None
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


# ---------------------------------------------------------------------------
# Constant value serialization (serialize_constant / deserialize_constant)
# ---------------------------------------------------------------------------


def serialize_constant(value: Any) -> dict[str, Any]:
    """Serialize a tool-parameter constant to a JSON-safe envelope.

    The output is a dict ``{"__type__": <name>, "value": <payload>}`` that
    round-trips through :func:`deserialize_constant`. This is the format
    used inside the ``constants`` block of a workflow's
    :meth:`Workflow.to_dict` output.

    Supported types and their envelopes:

    - ``bool``   → ``{"__type__": "bool", "value": <bool>}``
    - ``int``    → ``{"__type__": "int", "value": <int>}``
    - ``float``  → ``{"__type__": "float", "value": <float>}``
    - ``list``   → ``{"__type__": "list", "value": [...]}``
    - ``tuple``  → ``{"__type__": "tuple", "value": [...]}``
    - anything else (including :class:`pathlib.Path`, Pydantic models,
      enums, custom dataclasses) is **lossily** stringified via ``str()``
      and tagged ``{"__type__": "str", ...}``. Callers that need lossless
      round-trip for non-primitive values must serialize them at a
      higher layer.
    """
    if isinstance(value, bool):
        return {"__type__": "bool", "value": value}
    if isinstance(value, int):
        return {"__type__": "int", "value": value}
    if isinstance(value, float):
        return {"__type__": "float", "value": value}
    if isinstance(value, (list, tuple)):
        return {"__type__": type(value).__name__, "value": list(value)}
    return {"__type__": "str", "value": str(value)}


def deserialize_constant(data: dict[str, Any]) -> Any:
    """Inverse of :func:`serialize_constant`.

    Expects a typed envelope ``{"__type__": <name>, "value": <payload>}``
    produced by :func:`serialize_constant`. Unknown ``__type__`` values
    are coerced to ``str``.
    """
    t = data["__type__"]
    v = data["value"]
    if t == "bool":
        return bool(v)
    if t == "int":
        return int(v)
    if t == "float":
        return float(v)
    if t == "tuple":
        return tuple(v)
    if t == "list":
        return list(v)
    return str(v)


# ---------------------------------------------------------------------------
# Wire-format serialization (serialize_input_schema / serialize_output_schema)
# ---------------------------------------------------------------------------


def _unwrap_annotated(annotation: Any) -> Any:
    """Return the first argument of ``Annotated[...]``; pass through otherwise."""
    if get_origin(annotation) is Annotated:
        return get_args(annotation)[0]
    return annotation


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


def _is_nullable(annotation: Any) -> bool:
    """Return True iff ``annotation`` admits ``None`` as a value.

    Strips a leading ``Annotated[...]`` wrapper, then checks whether the inner
    type is a ``Union`` / ``X | Y`` whose args include ``NoneType``. Used by
    :func:`serialize_input_schema` to surface nullability to GUIs separately
    from ``required`` (which only reflects whether a class-level default exists
    on ``Inputs``).
    """
    inner = _unwrap_annotated(annotation)
    origin = get_origin(inner)
    if origin is Union or origin is UnionType:
        return type(None) in get_args(inner)
    return False


def _jsonify_default(value: Any) -> Any:
    """Convert a default value to a JSON-safe representation (§4.3).

    Rules:
    - ``None``, ``bool``, ``int``, ``float``, ``str`` → returned as-is.
    - :class:`pathlib.Path` → ``str(path)``.
    - :class:`~enum.Enum` member → ``str(member.value)``.
    - ``list`` / ``tuple`` → list of recursively-serialized elements.
    - ``dict`` → dict with string keys and recursively-serialized values.
    - Anything else → ``str(value)`` fallback.
    """
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Enum):
        return str(value.value)
    if isinstance(value, (list, tuple)):
        return [_jsonify_default(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _jsonify_default(v) for k, v in value.items()}
    return str(value)


def _display_type_name(annotation: Any) -> str:
    """Return the display-name string for ``annotation`` (§4.2).

    The string is the type label GUIs use for widget selection. It is not a
    runtime type — enumeration of values goes through :func:`_extract_choices`
    and ``None``-ability is expressed via ``required``.
    """
    # ``ImagePath(...)`` / ``ImageShared(...)`` are factory functions that
    # return ``Annotated[Path | SharedArray, ImageSpec(...)]``. Platform code
    # special-cases these names for widget selection, so we recognize them
    # before generic Annotated-unwrapping collapses them to the base type.
    if get_origin(annotation) is Annotated and extract_image_spec(annotation) is not None:
        base = get_args(annotation)[0]
        if base is Path:
            return "ImagePath"
        base_name = getattr(base, "__name__", None)
        if base_name == "SharedArray":
            return "ImageShared"

    inner = _unwrap_annotated(annotation)
    inner = _unwrap_optional(inner)
    # Optional may have wrapped an Annotated in turn (e.g. Optional[Annotated[int, ...]]).
    inner = _unwrap_annotated(inner)

    if inner is Path:
        return "Path"

    origin = get_origin(inner)
    if origin is Literal:
        args = get_args(inner)
        if not args:
            return "str"
        return type(args[0]).__name__

    if isinstance(inner, type) and issubclass(inner, Enum):
        return "str"

    if origin is list or inner is list:
        return "list"
    if origin is dict or inner is dict:
        return "dict"
    if origin is tuple or inner is tuple:
        return "tuple"

    if isinstance(inner, type):
        return inner.__name__

    return str(inner)


def _extract_choices(annotation: Any) -> list[str] | None:
    """Return the list of string choices for ``annotation`` (§4.6), or ``None``.

    Supports ``Literal[...]`` and :class:`~enum.Enum` subclasses, unwrapping
    ``Annotated[...]`` and ``Optional[...]`` first.
    """
    inner = _unwrap_annotated(annotation)
    inner = _unwrap_optional(inner)
    inner = _unwrap_annotated(inner)

    if get_origin(inner) is Literal:
        return [str(arg) for arg in get_args(inner)]

    if isinstance(inner, type) and issubclass(inner, Enum):
        return [str(member.value) for member in inner]

    return None


def _serialize_connectable(c: Connectable | None) -> str:
    """Return the lowercase string form of a :class:`Connectable` (§4.4).

    ``None`` is mapped to ``"not_by_default"``, matching the default value
    of :attr:`GUIMeta.connectable`.
    """
    if c is None:
        return Connectable.NOT_BY_DEFAULT.value
    return c.value


def serialize_input_schema(tool_class: type[BaseTool]) -> dict[str, dict[str, Any]]:
    """Return a JSON-serializable input schema for a tool.

    For each field declared on ``tool_class.Inputs`` (respecting MRO),
    returns a dict with the following keys:

    - ``type``: display-name string (e.g. ``"float"``, ``"Path"``,
      ``"ImagePath"``) — see :func:`_display_type_name`.
    - ``required``: ``True`` when no class-level default is set on
      ``Inputs`` for the field. Orthogonal to ``Optional[X]``.
    - ``connectable``: one of ``"never" | "not_by_default" | "by_default"``
      (from :class:`Connectable`).
    - ``default``: JSON-safe representation of the class-level default, or
      ``None`` when the field is required.
    - ``display_name``, ``description``, ``group``, ``min``, ``max``,
      ``step``: values from :class:`GUIMeta` (or ``None`` when absent).
    - ``choices``: list of strings for ``Literal[...]`` / :class:`Enum`
      fields, or ``None``.
    - ``image_spec``: dict produced by :func:`serialize_image_spec`, or
      ``None`` when the field has no :class:`ImageSpec`.

    Returns ``{}`` when ``tool_class`` has no ``Inputs`` class attribute.

    This is the canonical wire format consumed by GUIs (see the
    ``bioimageflow-platform`` ``GET /tools`` endpoint). Callers that want
    Python objects (raw ``type``, raw :class:`Connectable`) should use
    :func:`get_inputs_schema` instead.
    """
    inputs_cls = getattr(tool_class, "Inputs", None)
    if inputs_cls is None:
        return {}
    annotations = inputs_cls._get_all_annotations()
    schema: dict[str, dict[str, Any]] = {}

    for field_name, annotation in annotations.items():
        image_spec = extract_image_spec(annotation)
        gui_meta = extract_gui_meta(annotation)

        has_default = hasattr(inputs_cls, field_name)
        raw_default = getattr(inputs_cls, field_name, None) if has_default else None

        entry: dict[str, Any] = {
            "type": _display_type_name(annotation),
            "required": not has_default,
            "nullable": _is_nullable(annotation),
            "connectable": _serialize_connectable(
                gui_meta.connectable if gui_meta is not None else None
            ),
            "default": _jsonify_default(raw_default) if has_default else None,
            "display_name": gui_meta.display_name if gui_meta is not None else None,
            "description": gui_meta.description if gui_meta is not None else None,
            "group": gui_meta.group if gui_meta is not None else None,
            "min": gui_meta.min if gui_meta is not None else None,
            "max": gui_meta.max if gui_meta is not None else None,
            "step": gui_meta.step if gui_meta is not None else None,
            "choices": _extract_choices(annotation),
            "image_spec": serialize_image_spec(image_spec),
        }
        schema[field_name] = entry

    return schema


def _overrides_classmethod(cls: type, base: type, method_name: str) -> bool:
    """Return True if ``cls`` overrides ``base.<method_name>`` (a classmethod).

    Compares unwrapped function objects (``__func__``) so that inheritance
    of the same classmethod returns False, and a real override returns True.
    """
    own = getattr(cls, method_name, None)
    inherited = getattr(base, method_name, None)
    own_func = getattr(own, "__func__", None)
    base_func = getattr(inherited, "__func__", None)
    return own_func is not None and base_func is not None and own_func is not base_func


def serialize_tool_metadata(tool_class: type[BaseTool]) -> dict[str, Any]:
    """Return per-tool wire-format metadata for ``tool_class``. JSON-safe.

    Keys:

    - ``tool_type`` — ``"DataFrameTool"`` if ``tool_class`` is a subclass of
      :class:`bioimageflow.DataFrameTool`, otherwise ``"ProcessingTool"``.
    - ``accepts_upstream`` — ``True`` if the tool accepts positional upstream
      :class:`Node` arguments. Always ``True`` for ``ProcessingTool`` (whose
      column-bound inputs are upstream-equivalent for the GUI). For
      ``DataFrameTool`` it reflects ``tool_class.accepts_upstream``.
    - ``dynamic_outputs`` — ``True`` if the tool's output schema depends on
      its inputs (i.e. it overrides :meth:`DataFrameTool.resolve_outputs`).

    Companion to :func:`serialize_input_schema` / :func:`serialize_output_schema`,
    which describe per-field schemas. This helper exists so platform code does
    not have to perform ``issubclass`` checks against library types itself.
    """
    # Lazy import to keep validation.py independent of the orchestrator
    # subpackage at module load time.
    try:
        from bioimageflow.dataframe_tool import DataFrameTool
    except ImportError:  # pragma: no cover - defensive
        DataFrameTool = None  # type: ignore[assignment]

    if DataFrameTool is not None and isinstance(tool_class, type) and issubclass(tool_class, DataFrameTool):
        tool_type = "DataFrameTool"
        accepts_upstream = bool(getattr(tool_class, "accepts_upstream", True))
        # ``dynamic_outputs`` is True when the tool's resolved schema can
        # differ from a static ``serialize_output_schema(cls)`` — i.e. it
        # overrides either ``resolve_outputs`` (input-driven schema like
        # ``Generate``) or ``resolve_merge_schema`` (upstream-driven schema
        # on built-in merge tools).
        dynamic_outputs = (
            _overrides_classmethod(tool_class, DataFrameTool, "resolve_outputs")
            or _overrides_classmethod(tool_class, DataFrameTool, "resolve_merge_schema")
        )
    else:
        tool_type = "ProcessingTool"
        accepts_upstream = True
        dynamic_outputs = False

    return {
        "tool_type": tool_type,
        "accepts_upstream": accepts_upstream,
        "dynamic_outputs": dynamic_outputs,
    }


def serialize_resolved_outputs(node: Any) -> dict[str, Any]:
    """Resolve a configured node's output schema for the wire format. JSON-safe.

    Returns ``{"resolved": True, "columns": <schema>}`` when
    :meth:`bioimageflow.node.Node.get_output_schema` resolves; otherwise
    ``{"resolved": False, "columns": {}}``.

    GUIs use this to render per-column output pins on configured nodes
    (``Generate(column_name="x")`` or fully-configured merge tools). When
    ``resolved`` is ``False`` the GUI should render a placeholder pin and
    re-call after the user supplies more inputs.

    The ``columns`` dict has the same shape as
    :func:`serialize_output_schema` — either per-field entries or the
    ``{"_passthrough": True, ...}`` marker.
    """
    schema = node.get_output_schema()
    if schema is None:
        return {"resolved": False, "columns": {}}
    return {"resolved": True, "columns": schema}


def serialize_output_schema(tool_class: type[BaseTool]) -> dict[str, Any]:
    """Return a JSON-serializable output schema for a tool.

    Per-field shape::

        {"type": str, "default": Any | None, "image_spec": dict | None}

    Returns ``{}`` when ``tool_class`` has no ``Outputs`` class attribute.

    When ``Outputs`` is (or subclasses) :class:`bioimageflow.Passthrough`,
    the returned dict is the marker ``{"_passthrough": True}`` — GUIs
    should render this as "inherits upstream columns".
    """
    outputs_cls = getattr(tool_class, "Outputs", None)
    if outputs_cls is None:
        return {}

    # Passthrough marker (DataFrameTool): avoid importing bioimageflow.dataframe_tool
    # at module import time by resolving lazily.
    try:
        from bioimageflow.dataframe_tool import Passthrough
    except ImportError:  # pragma: no cover - defensive
        Passthrough = None  # type: ignore[assignment]

    if Passthrough is not None and isinstance(outputs_cls, type) and issubclass(outputs_cls, Passthrough):
        return {"_passthrough": True}

    annotations = outputs_cls._get_all_annotations()
    schema: dict[str, dict[str, Any]] = {}

    for field_name, annotation in annotations.items():
        image_spec = extract_image_spec(annotation)
        has_default = hasattr(outputs_cls, field_name)
        raw_default = getattr(outputs_cls, field_name, None) if has_default else None

        schema[field_name] = {
            "type": _display_type_name(annotation),
            "default": _jsonify_default(raw_default) if has_default else None,
            "image_spec": serialize_image_spec(image_spec),
        }

    return schema
