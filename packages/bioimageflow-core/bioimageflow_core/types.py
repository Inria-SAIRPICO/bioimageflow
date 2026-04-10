"""BioImageFlow type system — zero external dependencies."""

import warnings
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Annotated, Any, Set, Tuple, get_args, get_origin


class Semantic(str, Enum):
    """What the pixel values represent."""
    BINARY = "binary"
    LABEL = "label"
    INTENSITY = "intensity"
    PROBABILITY = "probability"
    DISPLACEMENT = "displacement"
    FEATURE = "feature"


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
    semantics: Set[Semantic] = field(default_factory=set)
    layouts: Set[Layout] = field(default_factory=set)
    dtypes: Set[str] = field(default_factory=set)
    formats: Set[str] = field(default_factory=set)


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


def ImagePath(
    semantics: Any = None,
    layouts: Any = None,
    dtypes: Any = None,
    formats: Any = None,
) -> Any:
    """Returns Annotated[Path, ImageSpec(...)]. Used for file-based image data."""
    spec = ImageSpec(
        semantics=_normalize_param(semantics),
        layouts=_normalize_param(layouts),
        dtypes=_normalize_param(dtypes),
        formats=_normalize_param(formats),
    )
    return Annotated[Path, spec]


def ImageShared(
    semantics: Any = None,
    layouts: Any = None,
    dtypes: Any = None,
) -> Any:
    """Returns Annotated[SharedArray, ImageSpec(...)]. Formats is implicitly {'memory'}."""
    spec = ImageSpec(
        semantics=_normalize_param(semantics),
        layouts=_normalize_param(layouts),
        dtypes=_normalize_param(dtypes),
        formats={"memory"},
    )
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
    """Declarative GUI hints for a tool input field.

    Attach to an ``Inputs`` annotation via ``Annotated`` to control how a
    GUI renders the field.

    Parameters
    ----------
    connectable : Connectable
        Controls whether and how this input can be bound to an upstream
        column.  Defaults to ``Connectable.NOT_BY_DEFAULT``.
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
    connectable: Connectable = Connectable.NOT_BY_DEFAULT
    min: float | None = None
    max: float | None = None
    step: float | None = None
    group: str | None = None


def extract_gui_meta(annotation: Any) -> GUIMeta | None:
    """Extract :class:`GUIMeta` from an ``Annotated`` type, or return ``None``."""
    if get_origin(annotation) is Annotated:
        for arg in get_args(annotation):
            if isinstance(arg, GUIMeta):
                return arg
    return None


def check_compatibility(producer_spec: ImageSpec, consumer_spec: ImageSpec) -> bool:
    """Returns True if the producer's output is acceptable for the consumer's input."""
    for attr in ["semantics", "layouts", "dtypes", "formats"]:
        producer_values: set[Any] = getattr(producer_spec, attr)
        consumer_values: set[Any] = getattr(consumer_spec, attr)
        if not consumer_values:
            continue
        if not producer_values:
            warnings.warn(f"Producer does not declare '{attr}'; cannot verify.")
            continue
        if not producer_values.intersection(consumer_values):
            return False
    return True
