"""BioImageFlow restoration tools."""

from importlib import import_module
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .baselines import BackgroundSubtract as BackgroundSubtract
    from .baselines import BilateralDenoise as BilateralDenoise
    from .baselines import GaussianDenoise as GaussianDenoise
    from .baselines import MedianDenoise as MedianDenoise
    from .baselines import RichardsonLucyRestoration as RichardsonLucyRestoration
    from .baselines import TotalVariationDenoise as TotalVariationDenoise
    from .baselines import UnsharpMask as UnsharpMask
    from .restore import CAREamicsPredict as CAREamicsPredict
    from .restore import RestorationMetrics as RestorationMetrics


_EXPORTS = {
    "BackgroundSubtract": ("baselines", "BackgroundSubtract"),
    "BilateralDenoise": ("baselines", "BilateralDenoise"),
    "CAREamicsPredict": ("restore", "CAREamicsPredict"),
    "GaussianDenoise": ("baselines", "GaussianDenoise"),
    "MedianDenoise": ("baselines", "MedianDenoise"),
    "RestorationMetrics": ("restore", "RestorationMetrics"),
    "RichardsonLucyRestoration": ("baselines", "RichardsonLucyRestoration"),
    "TotalVariationDenoise": ("baselines", "TotalVariationDenoise"),
    "UnsharpMask": ("baselines", "UnsharpMask"),
}

__all__ = [
    "BackgroundSubtract",
    "BilateralDenoise",
    "CAREamicsPredict",
    "GaussianDenoise",
    "MedianDenoise",
    "RestorationMetrics",
    "RichardsonLucyRestoration",
    "TotalVariationDenoise",
    "UnsharpMask",
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
