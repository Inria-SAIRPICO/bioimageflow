"""BioImageFlow object extraction and lightweight tracking tools."""

from importlib import import_module
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .linking import BTrackLink as BTrackLink
    from .labels import LabelsToObjects as LabelsToObjects
    from .linking import UltrackLink as UltrackLink
    from .metrics import TrackMetrics as TrackMetrics
    from .table_tools import FilterObjects as FilterObjects
    from .table_tools import TrackQualityMetrics as TrackQualityMetrics
    from .table_tools import TrackSummary as TrackSummary
    from .table_tools import TrackTableValidate as TrackTableValidate
    from .table_tools import TracksToLabels as TracksToLabels


_EXPORTS = {
    "BTrackLink": ("linking", "BTrackLink"),
    "FilterObjects": ("table_tools", "FilterObjects"),
    "LabelsToObjects": ("labels", "LabelsToObjects"),
    "TrackMetrics": ("metrics", "TrackMetrics"),
    "TrackQualityMetrics": ("table_tools", "TrackQualityMetrics"),
    "TrackSummary": ("table_tools", "TrackSummary"),
    "TrackTableValidate": ("table_tools", "TrackTableValidate"),
    "TracksToLabels": ("table_tools", "TracksToLabels"),
    "UltrackLink": ("linking", "UltrackLink"),
}

__all__ = [
    "BTrackLink",
    "FilterObjects",
    "LabelsToObjects",
    "TrackMetrics",
    "TrackQualityMetrics",
    "TrackSummary",
    "TrackTableValidate",
    "TracksToLabels",
    "UltrackLink",
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
