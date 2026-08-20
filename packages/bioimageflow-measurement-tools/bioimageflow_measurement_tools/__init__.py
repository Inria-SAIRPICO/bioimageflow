"""BioImageFlow Measurement Tools."""

from importlib import import_module
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .processing_tools import CountLabels as CountLabels
    from .processing_tools import DiceIoU as DiceIoU
    from .processing_tools import IntensityProperties as IntensityProperties
    from .processing_tools import LabelBenchmark as LabelBenchmark
    from .processing_tools import ObjectMatchingMetrics as ObjectMatchingMetrics
    from .processing_tools import RegionProperties as RegionProperties
    from .processing_tools import ShapeProperties as ShapeProperties
    from .table_tools import AggregatePerImage as AggregatePerImage
    from .table_tools import NormalizeFeatures as NormalizeFeatures
    from .table_tools import SummarizeTable as SummarizeTable


_EXPORTS = {
    "AggregatePerImage": ("table_tools", "AggregatePerImage"),
    "CountLabels": ("processing_tools", "CountLabels"),
    "DiceIoU": ("processing_tools", "DiceIoU"),
    "IntensityProperties": ("processing_tools", "IntensityProperties"),
    "LabelBenchmark": ("processing_tools", "LabelBenchmark"),
    "NormalizeFeatures": ("table_tools", "NormalizeFeatures"),
    "ObjectMatchingMetrics": ("processing_tools", "ObjectMatchingMetrics"),
    "RegionProperties": ("processing_tools", "RegionProperties"),
    "ShapeProperties": ("processing_tools", "ShapeProperties"),
    "SummarizeTable": ("table_tools", "SummarizeTable"),
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
