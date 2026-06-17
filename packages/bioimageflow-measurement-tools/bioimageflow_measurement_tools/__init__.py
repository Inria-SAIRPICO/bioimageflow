"""BioImageFlow Measurement Tools."""

from importlib import import_module
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .measurements import AggregatePerImage as AggregatePerImage
    from .measurements import CountLabels as CountLabels
    from .measurements import DiceIoU as DiceIoU
    from .measurements import IntensityProperties as IntensityProperties
    from .measurements import LabelBenchmark as LabelBenchmark
    from .measurements import NormalizeFeatures as NormalizeFeatures
    from .measurements import ObjectMatchingMetrics as ObjectMatchingMetrics
    from .measurements import RegionProperties as RegionProperties
    from .measurements import ShapeProperties as ShapeProperties
    from .measurements import SummarizeTable as SummarizeTable


_EXPORTS = {
    "AggregatePerImage": ("measurements", "AggregatePerImage"),
    "CountLabels": ("measurements", "CountLabels"),
    "DiceIoU": ("measurements", "DiceIoU"),
    "IntensityProperties": ("measurements", "IntensityProperties"),
    "LabelBenchmark": ("measurements", "LabelBenchmark"),
    "NormalizeFeatures": ("measurements", "NormalizeFeatures"),
    "ObjectMatchingMetrics": ("measurements", "ObjectMatchingMetrics"),
    "RegionProperties": ("measurements", "RegionProperties"),
    "ShapeProperties": ("measurements", "ShapeProperties"),
    "SummarizeTable": ("measurements", "SummarizeTable"),
}

__all__ = [
    "AggregatePerImage",
    "CountLabels",
    "DiceIoU",
    "IntensityProperties",
    "LabelBenchmark",
    "NormalizeFeatures",
    "ObjectMatchingMetrics",
    "RegionProperties",
    "ShapeProperties",
    "SummarizeTable",
]


def __getattr__(name: str) -> Any:
    try:
        module_name, attribute_name = _EXPORTS[name]
    except KeyError as exc:
        raise AttributeError(name) from exc
    module = import_module(f".{module_name}", __name__)
    value = getattr(module, attribute_name)
    globals()[name] = value
    return value
