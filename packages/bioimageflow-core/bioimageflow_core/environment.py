"""Environment and resource specifications."""

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class EnvironmentSpec:
    """Defines a reusable Wetlands environment specification."""
    name: str
    dependencies: dict[str, str | list[str]]


@dataclass(frozen=True)
class ResourceSpec:
    """Resource requirements for a processing tool."""
    cpu: int = 1
    gpu: int = 0
    gpu_memory: str | None = None
    max_concurrent: int = 0
    memory: str | None = None


class EnvironmentMismatchError(Exception):
    """Raised when two EnvironmentSpecs share a name but differ in dependencies."""
    pass
