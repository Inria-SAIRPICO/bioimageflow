"""BioImageFlow object extraction and lightweight tracking tools."""

from importlib import import_module
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .labels import LabelsToObjects as LabelsToObjects
    from .linking import NearestNeighborLink as NearestNeighborLink
    from .metrics import TrackMetrics as TrackMetrics
    from .rendering import TracksToLabels as TracksToLabels
    from .table_tools import FilterObjects as FilterObjects
    from .table_tools import TrackQualityMetrics as TrackQualityMetrics
    from .table_tools import TrackTableValidate as TrackTableValidate


_EXPORTS = {
    "FilterObjects": ("table_tools", "FilterObjects"),
    "LabelsToObjects": ("labels", "LabelsToObjects"),
    "NearestNeighborLink": ("linking", "NearestNeighborLink"),
    "TrackMetrics": ("metrics", "TrackMetrics"),
    "TrackQualityMetrics": ("table_tools", "TrackQualityMetrics"),
    "TrackTableValidate": ("table_tools", "TrackTableValidate"),
    "TracksToLabels": ("rendering", "TracksToLabels"),
}

__all__ = [
    "FilterObjects",
    "LabelsToObjects",
    "NearestNeighborLink",
    "TrackMetrics",
    "TrackQualityMetrics",
    "TrackTableValidate",
    "TracksToLabels",
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
