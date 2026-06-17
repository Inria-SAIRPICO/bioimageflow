"""BioImageFlow segmentation tools."""

from importlib import import_module
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .cellpose_v3 import Cellpose3 as Cellpose3
    from .classical import DistanceWatershedSegment as DistanceWatershedSegment
    from .classical import FilterLabels as FilterLabels
    from .classical import LocalThresholdSegment as LocalThresholdSegment
    from .classical import OtsuThresholdSegment as OtsuThresholdSegment
    from .classical import PostprocessLabels as PostprocessLabels
    from .classical import SplitTouchingObjects as SplitTouchingObjects
    from .classical import ThresholdSegment as ThresholdSegment
    from .classical import WatershedSegment as WatershedSegment
    from .stardist_segmenter import StarDistSegmenter as StarDistSegmenter


_EXPORTS = {
    "Cellpose3": ("cellpose_v3", "Cellpose3"),
    "DistanceWatershedSegment": ("classical", "DistanceWatershedSegment"),
    "FilterLabels": ("classical", "FilterLabels"),
    "LocalThresholdSegment": ("classical", "LocalThresholdSegment"),
    "OtsuThresholdSegment": ("classical", "OtsuThresholdSegment"),
    "PostprocessLabels": ("classical", "PostprocessLabels"),
    "SplitTouchingObjects": ("classical", "SplitTouchingObjects"),
    "StarDistSegmenter": ("stardist_segmenter", "StarDistSegmenter"),
    "ThresholdSegment": ("classical", "ThresholdSegment"),
    "WatershedSegment": ("classical", "WatershedSegment"),
}

__all__ = [
    "Cellpose3",
    "DistanceWatershedSegment",
    "FilterLabels",
    "LocalThresholdSegment",
    "OtsuThresholdSegment",
    "PostprocessLabels",
    "SplitTouchingObjects",
    "StarDistSegmenter",
    "ThresholdSegment",
    "WatershedSegment",
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
