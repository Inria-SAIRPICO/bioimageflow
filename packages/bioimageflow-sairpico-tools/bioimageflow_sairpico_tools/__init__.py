"""BioImageFlow wrappers for SAIRPICO command-line tools."""

from importlib import import_module
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .cimgdenoising import CImgDenoising as CImgDenoising
    from .hotspot import HotspotDetection as HotspotDetection
    from .hotspot import HotspotToSpots as HotspotToSpots
    from .simglib import GaussianPSF as GaussianPSF
    from .simglib import GibsonLanniPSF as GibsonLanniPSF
    from .simglib import MedianDenoising as MedianDenoising
    from .simglib import RichardsonLucyDeconvolution as RichardsonLucyDeconvolution
    from .simglib import SpitfireDeconvolution as SpitfireDeconvolution
    from .simglib import WienerDeconvolution as WienerDeconvolution


_EXPORTS = {
    "CImgDenoising": ("cimgdenoising", "CImgDenoising"),
    "GaussianPSF": ("simglib", "GaussianPSF"),
    "GibsonLanniPSF": ("simglib", "GibsonLanniPSF"),
    "HotspotDetection": ("hotspot", "HotspotDetection"),
    "HotspotToSpots": ("hotspot", "HotspotToSpots"),
    "MedianDenoising": ("simglib", "MedianDenoising"),
    "RichardsonLucyDeconvolution": (
        "simglib",
        "RichardsonLucyDeconvolution",
    ),
    "SpitfireDeconvolution": ("simglib", "SpitfireDeconvolution"),
    "WienerDeconvolution": ("simglib", "WienerDeconvolution"),
}

__all__ = [
    "CImgDenoising",
    "GaussianPSF",
    "GibsonLanniPSF",
    "HotspotDetection",
    "HotspotToSpots",
    "MedianDenoising",
    "RichardsonLucyDeconvolution",
    "SpitfireDeconvolution",
    "WienerDeconvolution",
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
