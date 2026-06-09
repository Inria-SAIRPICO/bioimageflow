"""Environment and resource specifications."""

from dataclasses import dataclass
from typing import Optional, Union


@dataclass(frozen=True)
class EnvironmentSpec:
    """Defines a reusable Wetlands environment specification."""
    name: str
    dependencies: dict[str, Union[str, list[str]]]


@dataclass(frozen=True)
class ResourceSpec:
    """Resource requirements for a processing tool."""
    cpu: int = 1
    gpu: int = 0
    gpu_memory: Optional[str] = None
    max_concurrent: int = 0
    memory: Optional[str] = None


class EnvironmentMismatchError(Exception):
    """Raised when two EnvironmentSpecs share a name but differ in dependencies."""
    pass


GENERAL_ENV = EnvironmentSpec(
    name="bioimageflow-general",
    dependencies={
        "python": "3.12",
        "pip": [
            "numpy",
            "scipy",
            "scikit-image",
            "imageio",
            "tifffile",
            "Pillow",
        ],
    },
)
