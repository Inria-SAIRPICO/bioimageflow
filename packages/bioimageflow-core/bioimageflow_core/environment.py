"""Environment and resource specifications."""

from dataclasses import dataclass
from typing import Any, Optional


@dataclass(frozen=True)
class EnvironmentSpec:
    """Defines a reusable Wetlands environment specification."""
    name: str
    dependencies: dict[str, Any]
    allow_flexible_versions: bool = False

    def __post_init__(self) -> None:
        """Reject package-index dependencies without explicit version constraints."""
        for section in ("pip", "conda"):
            dependencies = self.dependencies.get(section, [])
            if not dependencies:
                continue
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
    dependency = dependency.strip()
    dependency = dependency.split(";", 1)[0].strip()
    if not dependency:
        return False
    if " @ " in dependency:
        return True
    if section == "pip":
        return _pip_dependency_has_allowed_version_spec(
            dependency,
            allow_flexible=allow_flexible,
        )
    return _conda_dependency_has_allowed_version_spec(
        dependency,
        allow_flexible=allow_flexible,
    )


def _pip_dependency_has_allowed_version_spec(
    dependency: str,
    *,
    allow_flexible: bool,
) -> bool:
    if allow_flexible:
        return any(operator in dependency for operator in ("===", "==", "~=", ">=", "<=", "!=", ">", "<"))
    return "==" in dependency or "===" in dependency


def _conda_dependency_has_allowed_version_spec(
    dependency: str,
    *,
    allow_flexible: bool,
) -> bool:
    package_spec = dependency.split("::", 1)[-1]
    if allow_flexible:
        return any(operator in package_spec for operator in ("==", ">=", "<=", "!=", ">", "<", "="))
    if "==" in package_spec:
        return True
    return "=" in package_spec and not any(
        operator in package_spec for operator in (">=", "<=", "!=")
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
            "numpy==2.4.2",
            "scipy==1.17.1",
            "scikit-image==0.26.0",
            "imageio==2.37.3",
            "tifffile==2026.3.3",
            "Pillow==12.1.1",
            "pandas==3.0.1",
        ],
    },
)
