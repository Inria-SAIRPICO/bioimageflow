"""BioImageFlow restoration tools and synthetic benchmarks."""

from importlib import import_module
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .baselines import BackgroundSubtract as BackgroundSubtract
    from .baselines import GaussianDenoise as GaussianDenoise
    from .baselines import MedianDenoise as MedianDenoise
    from .baselines import RichardsonLucyRestoration as RichardsonLucyRestoration
    from .baselines import UnsharpMask as UnsharpMask
    from .benchmark import BenchmarkRestoration as BenchmarkRestoration
    from .restore import CAREamicsPredict as CAREamicsPredict
    from .restore import RestoreImage as RestoreImage
    from .restore import RestorationMetrics as RestorationMetrics


_EXPORTS = {
    "BackgroundSubtract": ("baselines", "BackgroundSubtract"),
    "BenchmarkRestoration": ("benchmark", "BenchmarkRestoration"),
    "CAREamicsPredict": ("restore", "CAREamicsPredict"),
    "GaussianDenoise": ("baselines", "GaussianDenoise"),
    "MedianDenoise": ("baselines", "MedianDenoise"),
    "RestoreImage": ("restore", "RestoreImage"),
    "RestorationMetrics": ("restore", "RestorationMetrics"),
    "RichardsonLucyRestoration": ("baselines", "RichardsonLucyRestoration"),
    "UnsharpMask": ("baselines", "UnsharpMask"),
}

__all__ = [
    "BackgroundSubtract",
    "BenchmarkRestoration",
    "CAREamicsPredict",
    "GaussianDenoise",
    "MedianDenoise",
    "RestoreImage",
    "RestorationMetrics",
    "RichardsonLucyRestoration",
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
