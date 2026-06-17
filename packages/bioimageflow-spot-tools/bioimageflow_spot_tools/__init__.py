"""BioImageFlow spot detection and puncta quantification tools."""

from importlib import import_module
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .assignment import AssignSpotsToLabels as AssignSpotsToLabels
    from .detection import DetectSpots as DetectSpots
    from .summary import SpotSummary as SpotSummary
    from .table_tools import FilterSpots as FilterSpots
    from .table_tools import RenderSpots as RenderSpots
    from .table_tools import SpotColocalization as SpotColocalization
    from .table_tools import SpotQualityMetrics as SpotQualityMetrics
    from .table_tools import SpotsToLabels as SpotsToLabels


_EXPORTS = {
    "AssignSpotsToLabels": ("assignment", "AssignSpotsToLabels"),
    "DetectSpots": ("detection", "DetectSpots"),
    "FilterSpots": ("table_tools", "FilterSpots"),
    "RenderSpots": ("table_tools", "RenderSpots"),
    "SpotColocalization": ("table_tools", "SpotColocalization"),
    "SpotQualityMetrics": ("table_tools", "SpotQualityMetrics"),
    "SpotSummary": ("summary", "SpotSummary"),
    "SpotsToLabels": ("table_tools", "SpotsToLabels"),
}

__all__ = [
    "AssignSpotsToLabels",
    "DetectSpots",
    "FilterSpots",
    "RenderSpots",
    "SpotColocalization",
    "SpotQualityMetrics",
    "SpotSummary",
    "SpotsToLabels",
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
