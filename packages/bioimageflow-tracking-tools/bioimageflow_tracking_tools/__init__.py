"""BioImageFlow object extraction and lightweight tracking tools."""

from importlib import import_module
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .labels import LabelsToObjects as LabelsToObjects
    from .linking import LinkObjects as LinkObjects
    from .metrics import TrackMetrics as TrackMetrics
    from .table_tools import FilterObjects as FilterObjects
    from .table_tools import TrackQualityMetrics as TrackQualityMetrics
    from .table_tools import TrackSummary as TrackSummary
    from .table_tools import TrackTableValidate as TrackTableValidate
    from .table_tools import TracksToLabels as TracksToLabels


_EXPORTS = {
    "FilterObjects": ("table_tools", "FilterObjects"),
    "LabelsToObjects": ("labels", "LabelsToObjects"),
    "LinkObjects": ("linking", "LinkObjects"),
    "TrackMetrics": ("metrics", "TrackMetrics"),
    "TrackQualityMetrics": ("table_tools", "TrackQualityMetrics"),
    "TrackSummary": ("table_tools", "TrackSummary"),
    "TrackTableValidate": ("table_tools", "TrackTableValidate"),
    "TracksToLabels": ("table_tools", "TracksToLabels"),
}

__all__ = [
    "FilterObjects",
    "LabelsToObjects",
    "LinkObjects",
    "TrackMetrics",
    "TrackQualityMetrics",
    "TrackSummary",
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
