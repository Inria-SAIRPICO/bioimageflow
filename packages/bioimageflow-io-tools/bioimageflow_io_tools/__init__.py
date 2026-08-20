"""BioImageFlow IO Tools."""

from importlib import import_module
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .bioio_convert import BioIOConvertImage as BioIOConvertImage
    from .metadata import ReadImageMetadata as ReadImageMetadata
    from .selection import SelectChannel as SelectChannel
    from .selection import SelectDimensions as SelectDimensions
    from .selection import SelectScene as SelectScene
    from .selection import SelectTimepoint as SelectTimepoint
    from .selection import SelectZRange as SelectZRange
    from .selection import ValidateImageLayout as ValidateImageLayout
    from .writers import ConvertImageFormat as ConvertImageFormat
    from .writers import ConvertToOmeTiff as ConvertToOmeTiff
    from .writers import ConvertToOmeZarr as ConvertToOmeZarr


_EXPORTS = {
    "BioIOConvertImage": ("bioio_convert", "BioIOConvertImage"),
    "ConvertImageFormat": ("writers", "ConvertImageFormat"),
    "ConvertToOmeTiff": ("writers", "ConvertToOmeTiff"),
    "ConvertToOmeZarr": ("writers", "ConvertToOmeZarr"),
    "ReadImageMetadata": ("metadata", "ReadImageMetadata"),
    "SelectChannel": ("selection", "SelectChannel"),
    "SelectDimensions": ("selection", "SelectDimensions"),
    "SelectScene": ("selection", "SelectScene"),
    "SelectTimepoint": ("selection", "SelectTimepoint"),
    "SelectZRange": ("selection", "SelectZRange"),
    "ValidateImageLayout": ("selection", "ValidateImageLayout"),
}

__all__ = [
    "BioIOConvertImage",
    "ConvertImageFormat",
    "ConvertToOmeTiff",
    "ConvertToOmeZarr",
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
