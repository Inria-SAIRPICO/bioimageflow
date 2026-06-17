"""BioImageFlow IO Tools."""

from importlib import import_module
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .image_io import ConvertImageFormat as ConvertImageFormat
    from .image_io import ConvertToOmeTiff as ConvertToOmeTiff
    from .image_io import ConvertToOmeZarr as ConvertToOmeZarr
    from .image_io import ReadImage as ReadImage
    from .image_io import ReadImageMetadata as ReadImageMetadata
    from .image_io import SelectChannel as SelectChannel
    from .image_io import SelectDimensions as SelectDimensions
    from .image_io import SelectScene as SelectScene
    from .image_io import SelectTimepoint as SelectTimepoint
    from .image_io import SelectZRange as SelectZRange
    from .image_io import ValidateImageLayout as ValidateImageLayout


_EXPORTS = {
    "ConvertImageFormat": ("image_io", "ConvertImageFormat"),
    "ConvertToOmeTiff": ("image_io", "ConvertToOmeTiff"),
    "ConvertToOmeZarr": ("image_io", "ConvertToOmeZarr"),
    "ReadImage": ("image_io", "ReadImage"),
    "ReadImageMetadata": ("image_io", "ReadImageMetadata"),
    "SelectChannel": ("image_io", "SelectChannel"),
    "SelectDimensions": ("image_io", "SelectDimensions"),
    "SelectScene": ("image_io", "SelectScene"),
    "SelectTimepoint": ("image_io", "SelectTimepoint"),
    "SelectZRange": ("image_io", "SelectZRange"),
    "ValidateImageLayout": ("image_io", "ValidateImageLayout"),
}

__all__ = [
    "ConvertImageFormat",
    "ConvertToOmeTiff",
    "ConvertToOmeZarr",
    "ReadImage",
    "ReadImageMetadata",
    "SelectChannel",
    "SelectDimensions",
    "SelectScene",
    "SelectTimepoint",
    "SelectZRange",
    "ValidateImageLayout",
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
