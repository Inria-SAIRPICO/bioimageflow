"""Authoritative bioimageflow-core dependency selection and validation."""

from __future__ import annotations

import logging
import os
import sys
import urllib.parse
import urllib.request
from importlib.metadata import PackageNotFoundError, version as _pkg_version
from pathlib import Path
from typing import Any

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - Python 3.10
    import tomli as tomllib

from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

from bioimageflow_core.environment import EnvironmentSpec as BioImageFlowEnvironmentSpec

logger = logging.getLogger("bioimageflow")

_CORE_SOURCE_ENV = "BIOIMAGEFLOW_CORE_SOURCE"


class CoreRequirementConflictError(ValueError):
    """A tool environment requests a divergent bioimageflow-core runtime."""


def _bioimageflow_core_pin() -> str:
    """Return the package requirement pinning bioimageflow-core."""
    try:
        return f"bioimageflow-core=={_pkg_version('bioimageflow-core')}"
    except PackageNotFoundError:
        logger.warning(
            "bioimageflow-core package metadata not found; "
            "tool environments will install the latest published version."
        )
        return "bioimageflow-core"


def _local_bioimageflow_core_project() -> Path | None:
    """Return the local bioimageflow-core project path when running from source."""
    try:
        import bioimageflow_core
    except ImportError:
        return None
    package_dir = Path(bioimageflow_core.__file__).resolve().parent
    project_dir = package_dir.parent
    pyproject = project_dir / "pyproject.toml"
    if pyproject.exists() and 'name = "bioimageflow-core"' in pyproject.read_text():
        return project_dir
    return None


def _validated_bioimageflow_core_project(
    value: str | Path,
    *,
    setting: str = _CORE_SOURCE_ENV,
) -> Path:
    """Return a validated local bioimageflow-core project directory."""

    if not str(value).strip():
        raise RuntimeError(f"{setting} must name a bioimageflow-core source directory.")
    project_dir = Path(value).expanduser().resolve()
    if not project_dir.is_dir():
        raise RuntimeError(
            f"{setting} points to {project_dir}, which is not an existing directory."
        )
    pyproject = project_dir / "pyproject.toml"
    if not pyproject.is_file():
        raise RuntimeError(
            f"{setting} points to {project_dir}, which has no pyproject.toml."
        )
    try:
        document = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise RuntimeError(
            f"{setting} points to {project_dir}, whose pyproject.toml cannot be read."
        ) from exc
    project = document.get("project")
    name = project.get("name") if isinstance(project, dict) else None
    if not isinstance(name, str) or canonicalize_name(name) != "bioimageflow-core":
        raise RuntimeError(
            f"{setting} points to {project_dir}, whose project name is not "
            "'bioimageflow-core'."
        )
    raw_version = project.get("version") if isinstance(project, dict) else None
    if not isinstance(raw_version, str):
        raise RuntimeError(
            f"{setting} points to {project_dir}, whose project version is unavailable."
        )
    try:
        Version(raw_version)
    except InvalidVersion as exc:
        raise RuntimeError(
            f"{setting} points to {project_dir}, whose project version "
            f"{raw_version!r} is invalid."
        ) from exc
    package_dir = project_dir / "bioimageflow_core"
    if not package_dir.is_dir():
        raise RuntimeError(
            f"{setting} points to {project_dir}, which has no bioimageflow_core package."
        )
    return project_dir


def _bioimageflow_core_source_from_environment() -> Path | None:
    value = os.environ.get(_CORE_SOURCE_ENV)
    if value is None or not value.strip():
        return None
    return _validated_bioimageflow_core_project(value)


def _bioimageflow_core_editable_dependency(project_dir: Path) -> dict[str, Any]:
    """Return BioImageFlow's portable local-dependency declaration."""
    return {
        "name": "bioimageflow-core",
        "path": str(project_dir),
        "editable": True,
    }


def _dependency_name(dependency: Any) -> str | None:
    if isinstance(dependency, dict):
        value = dependency.get("name")
        return value if isinstance(value, str) else None
    if not isinstance(dependency, str):
        return None
    value = dependency.split(";", 1)[0].strip()
    value = value.split(" @ ", 1)[0]
    for marker in ("===", "==", "~=", ">=", "<=", "!=", ">", "<", "="):
        value = value.split(marker, 1)[0]
    return value.strip()


def _is_core_dependency(dependency: Any) -> bool:
    name = _dependency_name(dependency)
    return name is not None and canonicalize_name(name) == "bioimageflow-core"


def _is_local_dependency(dependency: Any) -> bool:
    return isinstance(dependency, dict) and "path" in dependency


def _env_var_is_truthy(name: str) -> bool:
    value = os.environ.get(name, "")
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _configured_core_dependency() -> Any:
    source = _bioimageflow_core_source_from_environment()
    if source is not None:
        return _bioimageflow_core_editable_dependency(source)
    if _env_var_is_truthy("BIOIMAGEFLOW_USE_LOCAL_CORE"):
        project_dir = _local_bioimageflow_core_project()
        if project_dir is None:
            raise RuntimeError(
                "BIOIMAGEFLOW_USE_LOCAL_CORE requires a bioimageflow-core source checkout."
            )
        return _bioimageflow_core_editable_dependency(project_dir)
    return _bioimageflow_core_pin()


def _configured_core_version(dependency: Any = None) -> Version:
    if isinstance(dependency, str):
        try:
            requirement = Requirement(dependency)
        except InvalidRequirement:
            requirement = None
        if requirement is not None and requirement.url is None:
            exact_versions = [
                specifier.version
                for specifier in requirement.specifier
                if specifier.operator in {"==", "==="} and "*" not in specifier.version
            ]
            if len(exact_versions) == 1:
                try:
                    return Version(exact_versions[0])
                except InvalidVersion:
                    pass
    try:
        return Version(_pkg_version("bioimageflow-core"))
    except (PackageNotFoundError, InvalidVersion) as exc:
        raise CoreRequirementConflictError(
            "The active bioimageflow-core distribution version is unavailable."
        ) from exc


def _local_reference_path(requirement: Requirement) -> Path | None:
    if requirement.url is None:
        return None
    parsed = urllib.parse.urlparse(requirement.url)
    if parsed.scheme != "file" or parsed.query or parsed.fragment:
        return None
    if parsed.netloc not in {"", "localhost"}:
        return None
    return Path(
        urllib.request.url2pathname(urllib.parse.unquote(parsed.path))
    ).expanduser().resolve()


def _configured_local_core_path(dependency: Any) -> Path | None:
    if _is_local_dependency(dependency):
        return Path(str(dependency["path"])).expanduser().resolve()
    if isinstance(dependency, str):
        try:
            requirement = Requirement(dependency)
        except InvalidRequirement:
            return None
        return _local_reference_path(requirement)
    return None


def core_requirement_conflict(
    env_spec: BioImageFlowEnvironmentSpec,
    *,
    configured_dependency: Any | None = None,
) -> str | None:
    """Return why a tool's explicit core dependency cannot use the active core."""

    dependency = (
        _configured_core_dependency()
        if configured_dependency is None
        else configured_dependency
    )
    raw_conda = list(env_spec.dependencies.get("conda", ()))
    conda_core = [item for item in raw_conda if _is_core_dependency(item)]
    if conda_core:
        return (
            "bioimageflow-core is managed by BioImageFlow and cannot be declared "
            "as a Conda dependency."
        )

    raw_pip = list(env_spec.dependencies.get("pip", ()))
    raw_local = list(env_spec.dependencies.get("local", ()))
    explicit_pip = [item for item in raw_pip if _is_core_dependency(item)]
    explicit_local = [item for item in raw_local if _is_core_dependency(item)]
    if len(explicit_pip) + len(explicit_local) > 1:
        return "bioimageflow-core may be declared at most once in a tool environment."
    if not explicit_pip and not explicit_local:
        return None

    if explicit_local:
        declared = explicit_local[0]
        if not isinstance(declared, dict) or not _is_local_dependency(declared):
            return "The explicit bioimageflow-core local dependency is invalid."
        configured_path = _configured_local_core_path(dependency)
        if configured_path is None:
            return (
                "The tool requests a local bioimageflow-core checkout, but this "
                "BioImageFlow runtime uses a published core distribution."
            )
        declared_path = Path(str(declared["path"])).expanduser().resolve()
        if declared_path != configured_path:
            return (
                f"The tool requests bioimageflow-core from {declared_path}, but the "
                f"active runtime uses {configured_path}."
            )
        return None

    declared = explicit_pip[0]
    if not isinstance(declared, str):
        return "The explicit bioimageflow-core PyPI dependency must be a string."
    try:
        requirement = Requirement(declared)
    except InvalidRequirement:
        return f"The tool declares an invalid bioimageflow-core requirement: {declared!r}."

    if requirement.url is not None:
        configured_path = _configured_local_core_path(dependency)
        if configured_path is None:
            return (
                "The tool requests bioimageflow-core from a direct reference, but this "
                "BioImageFlow runtime uses a published core distribution."
            )
        declared_path = _local_reference_path(requirement)
        if declared_path != configured_path:
            return (
                f"The tool requests bioimageflow-core from {requirement.url!r}, but "
                f"the active runtime uses {configured_path}."
            )
        return None

    if _configured_local_core_path(dependency) is not None:
        return (
            "The tool requests a published bioimageflow-core distribution, but this "
            "BioImageFlow runtime uses a local core checkout."
        )
    if not requirement.specifier:
        return "The explicit bioimageflow-core requirement must constrain its version."
    version = _configured_core_version(dependency)
    if not requirement.specifier.contains(version, prereleases=True):
        return (
            f"The tool requires {requirement}, which is incompatible with the "
            f"active bioimageflow-core=={version}."
        )
    return None
