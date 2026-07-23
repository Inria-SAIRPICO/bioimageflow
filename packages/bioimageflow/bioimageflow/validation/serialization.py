"""Focused orchestrator validation behavior."""

from __future__ import annotations

from .common import (
    Annotated,
    Any,
    BaseTool,
    Connectable,
    Enum,
    IOModel,
    Literal,
    Path,
    Template,
    Union,
    UnionType,
    extract_gui_meta,
    get_args,
    get_origin,
)
from .schema import (
    _unwrap_optional,
    extract_image_spec,
    is_path_type,
    serialize_image_spec,
)


def _unwrap_annotated(annotation: Any) -> Any:
    """Return the first argument of ``Annotated[...]``; pass through otherwise."""
    if get_origin(annotation) is Annotated:
        return get_args(annotation)[0]
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
    if isinstance(value, Template):
        return value.pattern
    if isinstance(value, Enum):
        return str(value.value)
    if isinstance(value, (list, tuple)):
        return [_jsonify_default(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _jsonify_default(v) for k, v in value.items()}
    return str(value)


def validate_output_template_defaults(outputs_cls: type[IOModel]) -> None:
    """Validate explicit Template defaults on an Outputs model.

    ``Template(...)`` is only valid on path-typed output fields. Explicit
    string or ``Path`` defaults on path outputs are rejected, including static
    names like ``"fixed.tif"``. Use ``Template("fixed.tif")`` for static
    filenames, or omit the default to use generated naming.
    """
    annotations = outputs_cls._get_all_annotations()
    for field_name, annotation in annotations.items():
        if not hasattr(outputs_cls, field_name):
            continue

        default = getattr(outputs_cls, field_name)
        is_path_output = is_path_type(annotation)
        if isinstance(default, Template):
            if not is_path_output:
                raise TypeError(
                    f"Template default for '{field_name}' is only valid on "
                    f"path output fields."
                )
            continue

        if is_path_output and isinstance(default, (str, Path)):
            raise TypeError(
                f"Output path default for '{field_name}' must be declared "
                f"with Template(...), not a string or Path default."
            )


def _display_type_name(annotation: Any) -> str:
    """Return the display-name string for ``annotation`` (§4.2).

    The string is the type label GUIs use for widget selection. It is not a
    runtime type — enumeration of values goes through :func:`_extract_choices`
    and ``None``-ability is expressed via ``required``.
    """
    # Image annotations are ``Annotated[Path | SharedArray, ImageSpec(...)]``.
    # Platform code special-cases these names for widget selection, so we
    # recognize them before generic Annotated-unwrapping collapses them to the
    # base type.
    if (
        get_origin(annotation) is Annotated
        and extract_image_spec(annotation) is not None
    ):
        base = _unwrap_optional(get_args(annotation)[0])
        base_origin = get_origin(base)
        if base is Path or (
            (base_origin is Union or base_origin is UnionType)
            and Path in get_args(base)
        ):
            return "ImageFile"
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


def _add_gui_meta_fields(
    entry: dict[str, Any],
    gui_meta: Any,
    *,
    include_path_picker: bool = False,
) -> None:
    """Add JSON-safe GUIMeta fields to a serialized schema entry."""
    entry["connectable"] = _serialize_connectable(gui_meta.connectable)
    entry["display_name"] = gui_meta.display_name
    entry["description"] = gui_meta.description
    entry["group"] = gui_meta.group
    entry["min"] = gui_meta.min
    entry["max"] = gui_meta.max
    entry["step"] = gui_meta.step
    if include_path_picker:
        entry["path_picker"] = (
            gui_meta.path_picker.value if gui_meta.path_picker is not None else None
        )


def serialize_input_schema(tool_class: type[BaseTool]) -> dict[str, dict[str, Any]]:
    """Return a JSON-serializable input schema for a tool.

    For each field declared on ``tool_class.Inputs`` (respecting MRO),
    returns a dict with the following keys:

    - ``type``: display-name string (e.g. ``"float"``, ``"Path"``,
      ``"ImageFile"``) — see :func:`_display_type_name`.
    - ``required``: ``True`` when no class-level default is set on
      ``Inputs`` for the field. Orthogonal to ``Optional[X]``.
    - ``connectable``: one of ``"never" | "not_by_default" | "by_default"``
      (from :class:`Connectable`).
    - ``default``: JSON-safe representation of the class-level default, or
      ``None`` when the field is required.
    - ``display_name``, ``description``, ``group``, ``min``, ``max``,
      ``step``, ``path_picker``: values from :class:`GUIMeta` (or ``None``
      when absent).
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
            "default": _jsonify_default(raw_default) if has_default else None,
            "choices": _extract_choices(annotation),
            "image_spec": serialize_image_spec(image_spec),
        }
        if gui_meta is not None:
            _add_gui_meta_fields(entry, gui_meta, include_path_picker=True)
        else:
            entry.update(
                {
                    "connectable": _serialize_connectable(None),
                    "display_name": None,
                    "description": None,
                    "group": None,
                    "min": None,
                    "max": None,
                    "step": None,
                    "path_picker": None,
                }
            )
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
    - ``dataframe_output`` — ``True`` when nodes of this tool expose their
      full result DataFrame as a graph-level output. All current tool kinds
      produce a DataFrame at runtime; per-field output schemas still describe
      the DataFrame columns.

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

    if (
        DataFrameTool is not None
        and isinstance(tool_class, type)
        and issubclass(tool_class, DataFrameTool)
    ):
        tool_type = "DataFrameTool"
        accepts_upstream = bool(getattr(tool_class, "accepts_upstream", True))
        # ``dynamic_outputs`` is True when the tool's resolved schema can
        # differ from a static ``serialize_output_schema(cls)`` — i.e. it
        # overrides either ``resolve_outputs`` (input-driven schema like
        # ``Generate``) or ``resolve_merge_schema`` (upstream-driven schema
        # on built-in merge tools).
        dynamic_outputs = _overrides_classmethod(
            tool_class, DataFrameTool, "resolve_outputs"
        ) or _overrides_classmethod(tool_class, DataFrameTool, "resolve_merge_schema")
    else:
        tool_type = "ProcessingTool"
        accepts_upstream = True
        dynamic_outputs = False

    return {
        "tool_type": tool_type,
        "accepts_upstream": accepts_upstream,
        "dynamic_outputs": dynamic_outputs,
        "dataframe_output": True,
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

    if (
        Passthrough is not None
        and isinstance(outputs_cls, type)
        and issubclass(outputs_cls, Passthrough)
    ):
        return {"_passthrough": True}

    validate_output_template_defaults(outputs_cls)

    annotations = outputs_cls._get_all_annotations()
    schema: dict[str, dict[str, Any]] = {}

    for field_name, annotation in annotations.items():
        image_spec = extract_image_spec(annotation)
        gui_meta = extract_gui_meta(annotation)
        has_default = hasattr(outputs_cls, field_name)
        raw_default = getattr(outputs_cls, field_name, None) if has_default else None
        template = raw_default.pattern if isinstance(raw_default, Template) else None

        entry: dict[str, Any] = {
            "type": _display_type_name(annotation),
            "default": _jsonify_default(raw_default) if has_default else None,
            "image_spec": serialize_image_spec(image_spec),
        }
        if gui_meta is not None:
            _add_gui_meta_fields(entry, gui_meta)
        if template is not None:
            entry["template"] = template
        schema[field_name] = entry

    return schema
