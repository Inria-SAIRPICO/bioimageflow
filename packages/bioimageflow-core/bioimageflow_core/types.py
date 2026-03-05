"""BioImageFlow type system — zero external dependencies."""

import warnings
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Annotated, Any, Set, Tuple


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
