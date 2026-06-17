"""BioImageFlow worker-safe type system."""

import warnings
from collections.abc import Set as AbstractSet
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Annotated, Any, Optional, Tuple, get_args, get_origin


class Semantic(str, Enum):
    """What the pixel values represent."""
    BINARY = "binary"
    LABEL = "label"
    INTENSITY = "intensity"
    PROBABILITY = "probability"
    DISPLACEMENT = "displacement"
    FEATURE = "feature"


SCALAR_IMAGE_SEMANTICS = frozenset({
    Semantic.INTENSITY,
    Semantic.BINARY,
    Semantic.LABEL,
    Semantic.PROBABILITY,
})
"""Semantic values for scalar raster images.

Use this group for tools that consume displayable scalar images without
requiring a specific pixel meaning, such as visualization and montage tools.
It intentionally excludes vector fields and feature images.
"""


class Layout(str, Enum):
    """Axis ordering of the image data."""
    PLANAR = "YX"
    PLANAR_CHANNEL = "CYX"
    PLANAR_TIME = "TYX"
    PLANAR_TIME_CHANNEL = "TCYX"
    VOLUMETRIC = "ZYX"
    VOLUMETRIC_CHANNEL = "CZYX"
    VOLUMETRIC_TIME = "TZYX"
    VOLUMETRIC_TIME_CHANNEL = "TCZYX"

    @property
    def ndim(self) -> int:
        return len(self.value)


@dataclass(frozen=True)
class ImageSpec:
    """Defines type constraints. Empty sets mean 'any' (wildcard)."""
    if not TYPE_CHECKING:
        __hash__ = None

    semantics: AbstractSet[Semantic] = field(default_factory=frozenset)
    layouts: AbstractSet[Layout] = field(default_factory=frozenset)
    dtypes: AbstractSet[str] = field(default_factory=frozenset)
    formats: AbstractSet[str] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        object.__setattr__(self, "semantics", frozenset(self.semantics))
        object.__setattr__(self, "layouts", frozenset(self.layouts))
        object.__setattr__(self, "dtypes", frozenset(self.dtypes))
        object.__setattr__(self, "formats", frozenset(self.formats))


@dataclass(frozen=True)
class SharedArray:
    """A reference to data in shared memory. Picklable."""
    name: str
    shape: Tuple[int, ...]
    dtype: str


def _normalize_param(value: Any) -> set[Any]:
    """Convert single value or None to a set."""
    if value is None:
        return set()
    if isinstance(value, set):
        return value
    if isinstance(value, (list, tuple, frozenset)):
        return set(value)
    return {value}


def ImageShared(
    semantics: Any = None,
    layouts: Any = None,
    dtypes: Any = None,
    gui: Any = None,
) -> Any:
    """Returns Annotated[SharedArray, ImageSpec(...), optional GUIMeta]."""
    spec = ImageSpec(
        semantics=_normalize_param(semantics),
        layouts=_normalize_param(layouts),
        dtypes=_normalize_param(dtypes),
        formats={"memory"},
    )
    if gui is not None:
        return Annotated[SharedArray, spec, gui]
    return Annotated[SharedArray, spec]


class Connectable(Enum):
    """Whether a tool input field can be bound to an upstream dataframe column.

    - ``NEVER``: No input pin, no toggle — the field can never be wired.
    - ``NOT_BY_DEFAULT``: Pin hidden by default; a GUI checkbox reveals it.
    - ``BY_DEFAULT``: Pin visible by default; a GUI checkbox can hide it.
    """
    NEVER = "never"
    NOT_BY_DEFAULT = "not_by_default"
    BY_DEFAULT = "by_default"


@dataclass(frozen=True)
class GUIMeta:
    """Declarative GUI hints for a tool ``Inputs`` / ``Outputs`` field.

    Attach to an ``Inputs`` or ``Outputs`` annotation via ``Annotated`` to
    control how a GUI renders the field (label, tooltip, widget range, pin
    visibility, tab grouping, ...).

    Parameters
    ----------
    display_name : str | None
        Human-readable label shown next to the field in the GUI.  If
        ``None``, frontends should fall back to the field name
        (optionally prettified).
    description : str | None
        Longer help text (tooltip / inline help) explaining what the
        field means and how it is used.
    connectable : Connectable
        Controls whether and how an ``Inputs`` field can be bound to an
        upstream column.  Ignored for ``Outputs`` fields.  Defaults to
        ``Connectable.NOT_BY_DEFAULT``.
    min : float | None
        Minimum allowed value (numeric fields only).
    max : float | None
        Maximum allowed value (numeric fields only).
    step : float | None
        Step increment for spinbox / slider widgets (numeric fields only).
    group : str | None
        Logical group name for organising fields into tabs or sections
        (e.g. ``"general"``, ``"advanced"``, ``"gpu"``).  ``None`` means
        the field belongs to the default / unnamed group.
    """
    display_name: Optional[str] = None
    description: Optional[str] = None
    connectable: Connectable = Connectable.NOT_BY_DEFAULT
    min: Optional[float] = None
    max: Optional[float] = None
    step: Optional[float] = None
    group: Optional[str] = None


def extract_gui_meta(annotation: Any) -> Optional[GUIMeta]:
    """Extract :class:`GUIMeta` from an ``Annotated`` type, or return ``None``."""
    if get_origin(annotation) is Annotated:
        for arg in get_args(annotation):
            if isinstance(arg, GUIMeta):
                return arg
    return None


def check_compatibility(producer_spec: ImageSpec, consumer_spec: ImageSpec) -> bool:
    """Returns True if the producer's output is acceptable for the consumer's input."""
    for attr in ["semantics", "layouts", "dtypes", "formats"]:
        producer_values: AbstractSet[Any] = getattr(producer_spec, attr)
        consumer_values: AbstractSet[Any] = getattr(consumer_spec, attr)
        if not consumer_values:
            continue
        if not producer_values:
            warnings.warn(f"Producer does not declare '{attr}'; cannot verify.")
            continue
        if producer_values.isdisjoint(consumer_values):
            return False
    return True
