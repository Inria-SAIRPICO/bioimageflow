"""Environment and resource specifications."""

from dataclasses import dataclass, field
import re
from typing import Any, Optional
from urllib.parse import urlsplit

from packaging.requirements import InvalidRequirement, Requirement


@dataclass(frozen=True, init=False)
class EnvironmentSpec:
    """Defines a reusable environment with a privately captured recipe."""
    name: str
    _dependencies: dict[str, Any] = field(repr=False)
    allow_flexible_versions: bool = False

    def __init__(
        self,
        name: str,
        dependencies: dict[str, Any],
        allow_flexible_versions: bool = False,
    ) -> None:
        from .defaults import snapshot_value

        if not isinstance(dependencies, dict):
            raise TypeError("EnvironmentSpec dependencies must be a dictionary.")
        captured = snapshot_value(dependencies)
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "_dependencies", captured)
        object.__setattr__(self, "allow_flexible_versions", allow_flexible_versions)
        self._validate_dependencies()

    @property
    def dependencies(self) -> dict[str, Any]:
        """Return a detached ordinary recipe without exposing held state."""
        from .defaults import snapshot_value

        return snapshot_value(self._dependencies)

    def __repr__(self) -> str:
        return (
            f"EnvironmentSpec(name={self.name!r}, dependencies={self._dependencies!r}, "
            f"allow_flexible_versions={self.allow_flexible_versions!r})"
        )

    def _validate_dependencies(self) -> None:
        for section in ("pip", "conda"):
            dependencies = self._dependencies.get(section, [])
            if not isinstance(dependencies, list):
                raise TypeError(
                    f"EnvironmentSpec '{self.name}' dependency section '{section}' "
                    "must be a list."
                )
            for dependency in dependencies:
                if isinstance(dependency, dict):
                    continue
                if not isinstance(dependency, str):
                    raise TypeError(
                        f"EnvironmentSpec '{self.name}' dependency section "
                        f"'{section}' contains unsupported dependency "
                        f"{dependency!r}."
                    )
                if not _dependency_has_allowed_version_spec(
                    dependency,
                    section=section,
                    allow_flexible=self.allow_flexible_versions,
                ):
                    mode = (
                        "an explicit version constraint"
                        if self.allow_flexible_versions
                        else "an exact version pin"
                    )
                    raise ValueError(
                        f"EnvironmentSpec '{self.name}' dependency section "
                        f"'{section}' contains dependency "
                        f"{dependency!r}; expected {mode}."
                    )


def _dependency_has_allowed_version_spec(
    dependency: str,
    *,
    section: str,
    allow_flexible: bool,
) -> bool:
    if section == "pip":
        return _pip_dependency_has_allowed_version_spec(
            dependency, allow_flexible=allow_flexible,
        )
    return _conda_dependency_has_allowed_version_spec(
        dependency, allow_flexible=allow_flexible,
    )


def _pip_dependency_has_allowed_version_spec(
    dependency: str,
    *,
    allow_flexible: bool,
) -> bool:
    try:
        requirement = Requirement(dependency)
    except InvalidRequirement:
        return False
    if requirement.url:
        return True
    specifiers = tuple(requirement.specifier)
    if allow_flexible:
        return bool(specifiers)
    return any(
        specifier.operator in {"==", "==="} and "*" not in specifier.version
        for specifier in specifiers
    )


_CONDA_PACKAGE = re.compile(
    r"(?P<name>[A-Za-z0-9_][A-Za-z0-9_.-]*)(?P<constraint>.*)"
)
_CONDA_VERSION = r"[A-Za-z0-9*?][A-Za-z0-9._+!*?-]*"
_CONDA_PIN = re.compile(rf"(?P<operator>==|=)(?P<version>{_CONDA_VERSION})(?:=(?P<build>{_CONDA_VERSION}))?")
_CONDA_CONSTRAINT = re.compile(rf"(?:==|!=|>=|<=|>|<|=|~=){_CONDA_VERSION}")


def _conda_channel_is_valid(channel: str) -> bool:
    if re.fullmatch(r"[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*", channel):
        return all(part not in {".", ".."} for part in channel.split("/"))
    if any(char.isspace() for char in channel):
        return False
    try:
        url = urlsplit(channel)
        url.port  # Reject malformed network ports as part of URL admission.
    except ValueError:
        return False
    if url.query or url.fragment:
        return False
    if url.scheme in {"http", "https"}:
        return bool(url.hostname)
    return url.scheme == "file" and url.path.startswith("/")


def _conda_dependency_has_allowed_version_spec(
    dependency: str,
    *,
    allow_flexible: bool,
) -> bool:
    dependency = dependency.strip()
    if "::" in dependency:
        channel, dependency = dependency.split("::", 1)
        if not _conda_channel_is_valid(channel):
            return False
    package = _CONDA_PACKAGE.fullmatch(dependency)
    if package is None:
        return False
    constraint = package.group("constraint").strip()
    pin = _CONDA_PIN.fullmatch(constraint)
    if pin is not None:
        if allow_flexible:
            return True
        fixed = not any(char in constraint for char in "*?")
        return fixed and (pin.group("operator") == "==" or pin.group("build") is not None)
    return allow_flexible and bool(constraint) and all(
        _CONDA_CONSTRAINT.fullmatch(part.strip()) is not None
        for part in constraint.split(",")
    )


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
            "numpy==2.5.0",
            "scipy==1.18.0",
            "scikit-image==0.26.0",
            "imageio==2.37.3",
            "tifffile==2026.6.1",
            "Pillow==12.3.0",
            "pandas==3.0.3",
        ],
    },
)
