"""Orchestrator-side Wetlands 2 environment management."""

from __future__ import annotations

import logging
import os
import re
import threading
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass
from enum import Enum
from importlib.metadata import PackageNotFoundError, version as _pkg_version
from pathlib import Path
from typing import Any, Literal

from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

from bioimageflow_core.environment import EnvironmentSpec as BioImageFlowEnvironmentSpec
from bioimageflow.paths import get_wetlands_path
from wetlands import (
    EnvironmentManager,
    EnvironmentSpec,
    LocalPackage,
    ManagedEnvironment,
    OperationEvent,
    WorkerPool,
)

logger = logging.getLogger("bioimageflow")

_WORKER_TARGET = "bioimageflow_core.worker:execute_processing_task"
_LOCAL_PYPI_REFERENCE = re.compile(
    r"^\s*(?P<name>[A-Za-z0-9_.-]+)"
    r"(?:\[(?P<extras>[A-Za-z0-9_.,-]+)\])?"
    r"\s*@\s*(?P<url>file://\S+)\s*$"
)


class CoreRequirementConflictError(ValueError):
    """A tool environment requests a divergent bioimageflow-core runtime."""


class EnvironmentRecipeState(Enum):
    """Relationship between a requested recipe and Wetlands-managed state."""

    MISSING = "missing"
    CURRENT = "current"
    STALE = "stale"


@dataclass(frozen=True)
class EnvironmentPreparation:
    """One observable processing-environment preparation decision."""

    name: str
    action: Literal["creating", "updating", "starting", "reusing"]
    requested_recipe_hash: str
    existing_recipe_hash: str | None = None


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
    """Return why a tool's explicit core dependency cannot use the active core.

    BioImageFlow owns the worker-side core dependency. Compatible tool-declared
    constraints are accepted as validation intent, but the manager still installs
    its exact configured dependency so workers cannot silently diverge.
    """

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


def _manager_config(
    *,
    root: str | Path | None = None,
    pixi_executable: str | Path | None = None,
    network: Mapping[str, str] | None = None,
    termination_grace: float | None = None,
) -> dict[str, Any]:
    """Return only explicitly configured Wetlands manager values."""
    config: dict[str, Any] = {}
    if root is not None:
        config["root"] = root
    if pixi_executable is not None:
        config["pixi_executable"] = pixi_executable
    if network is not None:
        config["network"] = network
    if termination_grace is not None:
        config["termination_grace"] = termination_grace
    return config


_shared_manager: EnvironmentManager | None = None
_shared_manager_lock = threading.Lock()
_wetlands_config: dict[str, Any] = {}


def configure_wetlands(
    root: str | Path | None = None,
    *,
    pixi_executable: str | Path | None = None,
    network: Mapping[str, str] | None = None,
    termination_grace: float | None = None,
) -> None:
    """Configure the process-wide Wetlands 2 manager before first use."""
    global _wetlands_config
    config = _manager_config(
        root=root,
        pixi_executable=pixi_executable,
        network=network,
        termination_grace=termination_grace,
    )
    with _shared_manager_lock:
        if _shared_manager is not None:
            logger.warning(
                "Wetlands already initialized; ignoring configure_wetlands() call."
            )
            return
        _wetlands_config = config


def get_shared_environment_manager(
    root: str | Path | None = None,
    *,
    pixi_executable: str | Path | None = None,
    network: Mapping[str, str] | None = None,
    termination_grace: float | None = None,
) -> EnvironmentManager:
    """Return the lazily created process-wide Wetlands 2 manager."""
    global _shared_manager
    if _shared_manager is not None:
        return _shared_manager
    with _shared_manager_lock:
        if _shared_manager is None:
            supplied = _manager_config(
                root=root,
                pixi_executable=pixi_executable,
                network=network,
                termination_grace=termination_grace,
            )
            merged = {**_wetlands_config, **supplied}
            merged.setdefault("root", get_wetlands_path())
            _shared_manager = EnvironmentManager(**merged)
        return _shared_manager


def _reset_shared_manager() -> None:
    """Reset shared state for isolated tests."""
    global _shared_manager, _wetlands_config
    manager = _shared_manager
    _shared_manager = None
    _wetlands_config = {}
    if manager is not None:
        try:
            manager.close()
        except Exception:
            logger.debug("Failed to close test Wetlands manager", exc_info=True)


def _translate_conda(
    values: list[Any],
    channels: list[str] | None,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    translated: list[str] = []
    prefix_channels: list[str] = []
    for value in values:
        if not isinstance(value, str):
            raise TypeError(f"Wetlands 2 Conda dependencies must be strings, got {value!r}.")
        if "::" in value:
            channel, value = value.split("::", 1)
            if channel:
                prefix_channels.append(channel)
        translated.append(value)
    ordered_channels = (
        [*prefix_channels, "conda-forge"]
        if channels is None
        else [*channels, *prefix_channels]
    )
    return tuple(translated), tuple(dict.fromkeys(ordered_channels))


def _translate_local_dependency(value: Any) -> LocalPackage:
    if not isinstance(value, dict) or "path" not in value:
        raise TypeError("Wetlands 2 local dependencies require a mapping with 'path'.")
    package = LocalPackage(
        source=Path(str(value["path"])),
        editable=bool(value.get("editable", False)),
        extras=tuple(value.get("extras", ())),
    )
    declared = value.get("name")
    if isinstance(declared, str):
        canonical = declared.replace("_", "-").lower()
        if canonical != package.distribution_name:
            raise ValueError(
                f"Local dependency declares {declared!r}, but its project is "
                f"{package.distribution_name!r}."
            )
    return package


def _translate_pypi_dependencies(
    values: list[Any],
) -> tuple[tuple[str, ...], tuple[LocalPackage, ...]]:
    pypi: list[str] = []
    local: list[LocalPackage] = []
    for value in values:
        if not isinstance(value, str):
            raise TypeError("Wetlands 2 PyPI dependencies must be strings.")
        match = _LOCAL_PYPI_REFERENCE.fullmatch(value)
        if match is None:
            pypi.append(value)
            continue
        parsed = urllib.parse.urlparse(match.group("url"))
        if parsed.scheme != "file" or parsed.query or parsed.fragment:
            raise ValueError(f"Invalid local file dependency {value!r}.")
        if parsed.netloc not in {"", "localhost"}:
            raise ValueError("Local file dependencies must not use a remote host.")
        source = Path(
            urllib.request.url2pathname(urllib.parse.unquote(parsed.path))
        )
        extras = tuple(
            extra
            for extra in (match.group("extras") or "").split(",")
            if extra
        )
        package = LocalPackage(source=source, extras=extras)
        expected = re.sub(r"[-_.]+", "-", match.group("name")).lower()
        if expected != package.distribution_name:
            raise ValueError(
                f"Local dependency declares {match.group('name')!r}, but its "
                f"project is {package.distribution_name!r}."
            )
        local.append(package)
    return tuple(pypi), tuple(local)


class WetlandsEnvManager:
    """Provision Wetlands 2 environments and own their warm worker pools."""

    def __init__(
        self,
        root: str | Path | None = None,
        *,
        pixi_executable: str | Path | None = None,
        network: Mapping[str, str] | None = None,
        termination_grace: float | None = None,
        bioimageflow_core_dependency: Any | None = None,
        use_local_bioimageflow_core: bool | None = None,
    ) -> None:
        self._manager = get_shared_environment_manager(
            root=root,
            pixi_executable=pixi_executable,
            network=network,
            termination_grace=termination_grace,
        )
        self._environments: dict[str, ManagedEnvironment] = {}
        self._pools: dict[str, WorkerPool] = {}
        self._pool_configs: dict[str, tuple[int, float | None]] = {}
        self._specs: dict[str, EnvironmentSpec] = {}
        self._lock = threading.RLock()
        if bioimageflow_core_dependency is not None:
            self._bioimageflow_core_dependency = bioimageflow_core_dependency
        else:
            if use_local_bioimageflow_core is None:
                use_local_bioimageflow_core = _env_var_is_truthy(
                    "BIOIMAGEFLOW_USE_LOCAL_CORE"
                )
            self._bioimageflow_core_dependency = self._default_core_dependency(
                use_local_bioimageflow_core=use_local_bioimageflow_core
            )

    @staticmethod
    def _default_core_dependency(*, use_local_bioimageflow_core: bool) -> Any:
        if not use_local_bioimageflow_core:
            return _bioimageflow_core_pin()
        project_dir = _local_bioimageflow_core_project()
        if project_dir is None:
            raise RuntimeError(
                "use_local_bioimageflow_core=True requires a source checkout."
            )
        return _bioimageflow_core_editable_dependency(project_dir)

    def _augment_dependencies(self, dependencies: dict[str, Any]) -> dict[str, Any]:
        """Return an independent BioImageFlow declaration including core."""
        deps = deepcopy(dependencies)
        env_spec = BioImageFlowEnvironmentSpec(
            name="validation",
            dependencies=deps,
            allow_flexible_versions=True,
        )
        conflict = core_requirement_conflict(
            env_spec,
            configured_dependency=self._bioimageflow_core_dependency,
        )
        if conflict is not None:
            raise CoreRequirementConflictError(conflict)
        pip_deps = [
            item for item in deps.get("pip", []) if not _is_core_dependency(item)
        ]
        local_deps = [
            item for item in deps.get("local", []) if not _is_core_dependency(item)
        ]
        if _is_local_dependency(self._bioimageflow_core_dependency):
            local_deps.append(deepcopy(self._bioimageflow_core_dependency))
        else:
            pip_deps.append(self._bioimageflow_core_dependency)
        deps["pip"] = pip_deps
        if local_deps:
            deps["local"] = local_deps
        else:
            deps.pop("local", None)
        return deps

    def _to_wetlands_spec(
        self,
        env_spec: BioImageFlowEnvironmentSpec,
    ) -> EnvironmentSpec:
        dependencies = self._augment_dependencies(env_spec.dependencies)
        allowed = {"python", "conda", "pip", "channels", "local"}
        unknown = sorted(set(dependencies).difference(allowed))
        if unknown:
            raise ValueError(
                "Unsupported BioImageFlow environment dependency section(s) for "
                f"Wetlands 2: {', '.join(unknown)}."
            )
        python = dependencies.get("python", ">=3.9")
        if not isinstance(python, str):
            raise TypeError("Environment 'python' must be a version string.")
        if re.fullmatch(r"[0-9]+\.[0-9]+", python):
            python = f"{python}.*"
        raw_conda = list(dependencies.get("conda", []))
        raw_channels = (
            list(dependencies["channels"]) if "channels" in dependencies else None
        )
        if raw_channels is not None and any(
            not isinstance(channel, str) for channel in raw_channels
        ):
            raise TypeError("Environment channels must be strings.")
        conda, channels = _translate_conda(raw_conda, raw_channels)
        pypi, local_from_pypi = _translate_pypi_dependencies(
            list(dependencies.get("pip", ()))
        )
        explicit_local = tuple(
            _translate_local_dependency(item)
            for item in dependencies.get("local", ())
        )
        local = local_from_pypi + explicit_local
        return EnvironmentSpec(
            python=python,
            conda=conda,
            pypi=pypi,
            channels=channels,
            local=local,
        )

    def _recipe_state(
        self,
        name: str,
        wetlands_spec: EnvironmentSpec,
    ) -> tuple[EnvironmentRecipeState, str | None]:
        infos = self._manager.managed_environments()
        info = next((candidate for candidate in infos if candidate.name == name), None)
        if info is None or not info.ready or info.recipe_hash is None:
            return EnvironmentRecipeState.MISSING, None
        if info.recipe_hash == wetlands_spec.recipe_hash:
            return EnvironmentRecipeState.CURRENT, info.recipe_hash
        return EnvironmentRecipeState.STALE, info.recipe_hash

    def inspect_environment(
        self,
        env_spec: BioImageFlowEnvironmentSpec,
    ) -> EnvironmentRecipeState:
        """Inspect a requested recipe without provisioning or starting workers."""

        wetlands_spec = self._to_wetlands_spec(env_spec)
        with self._lock:
            running_spec = self._specs.get(env_spec.name)
            if running_spec is not None:
                return (
                    EnvironmentRecipeState.CURRENT
                    if running_spec == wetlands_spec
                    else EnvironmentRecipeState.STALE
                )
            state, _ = self._recipe_state(env_spec.name, wetlands_spec)
            return state

    def get_or_create(
        self,
        env_spec: BioImageFlowEnvironmentSpec,
        max_workers: int = 1,
        worker_timeout: float | None = None,
        *,
        on_provision_event: Callable[[OperationEvent], None] | None = None,
        replace_existing: bool = False,
        on_preparation: Callable[[EnvironmentPreparation], None] | None = None,
    ) -> WorkerPool:
        """Provision an environment and return its cached Wetlands 2 pool.

        ``on_provision_event`` receives Wetlands setup events, including sanitized
        Pixi output, before this method waits for provisioning to finish. It is
        unused when an already running pool is returned. ``replace_existing``
        requests replacement of a Wetlands-managed environment, including one
        with the current recipe; it never authorizes mutation of an unmanaged target.
        """
        wetlands_spec = self._to_wetlands_spec(env_spec)
        config = (max_workers, worker_timeout)
        with self._lock:
            preparation_published = False
            existing = self._pools.get(env_spec.name)
            if existing is not None:
                if replace_existing or self._specs[env_spec.name] != wetlands_spec:
                    if not replace_existing:
                        raise ValueError(
                            f"Environment {env_spec.name!r} was already provisioned "
                            "with a different recipe."
                        )
                    existing_hash = self._specs[env_spec.name].recipe_hash
                    if on_preparation is not None:
                        on_preparation(
                            EnvironmentPreparation(
                                name=env_spec.name,
                                action="updating",
                                requested_recipe_hash=wetlands_spec.recipe_hash,
                                existing_recipe_hash=existing_hash,
                            )
                        )
                        preparation_published = True
                    self.stop(env_spec.name)
                    existing = None
                else:
                    if self._pool_configs[env_spec.name] != config:
                        raise ValueError(
                            f"Environment {env_spec.name!r} already has a pool with "
                            f"workers={self._pool_configs[env_spec.name][0]} and "
                            f"worker_timeout={self._pool_configs[env_spec.name][1]}."
                        )
                    if on_preparation is not None:
                        on_preparation(
                            EnvironmentPreparation(
                                name=env_spec.name,
                                action="reusing",
                                requested_recipe_hash=wetlands_spec.recipe_hash,
                                existing_recipe_hash=wetlands_spec.recipe_hash,
                            )
                        )
                    return existing
            state, existing_hash = self._recipe_state(env_spec.name, wetlands_spec)
            action: Literal["creating", "updating", "starting"]
            if replace_existing and state is not EnvironmentRecipeState.MISSING:
                action = "updating"
            elif state is EnvironmentRecipeState.STALE:
                action = "updating"
            elif state is EnvironmentRecipeState.CURRENT:
                action = "starting"
            else:
                action = "creating"
            if on_preparation is not None and not preparation_published:
                on_preparation(
                    EnvironmentPreparation(
                        name=env_spec.name,
                        action=action,
                        requested_recipe_hash=wetlands_spec.recipe_hash,
                        existing_recipe_hash=existing_hash,
                    )
                )
            operation = self._manager.provision(
                env_spec.name,
                wetlands_spec,
                replace_existing=replace_existing,
            )
            if on_provision_event is not None:
                operation.listen(on_provision_event)
            environment = operation.wait_for()
            pool = environment.start(
                workers=max_workers,
                worker_timeout=worker_timeout,
            )
            self._environments[env_spec.name] = environment
            self._pools[env_spec.name] = pool
            self._pool_configs[env_spec.name] = config
            self._specs[env_spec.name] = wetlands_spec
            return pool

    def submit_processing_task(
        self,
        env_spec: BioImageFlowEnvironmentSpec,
        payload: dict[str, Any],
        max_workers: int = 1,
        worker_timeout: float | None = None,
    ) -> Any:
        pool = self.get_or_create(
            env_spec,
            max_workers=max_workers,
            worker_timeout=worker_timeout,
        )
        return pool.submit_import(
            _WORKER_TARGET,
            args=(payload,),
            context_keyword="task",
        )

    def map_processing_tasks(
        self,
        env_spec: BioImageFlowEnvironmentSpec,
        payloads: list[dict[str, Any]],
        max_workers: int = 1,
        worker_timeout: float | None = None,
    ) -> list[Any]:
        return [
            self.submit_processing_task(
                env_spec,
                payload,
                max_workers=max_workers,
                worker_timeout=worker_timeout,
            )
            for payload in payloads
        ]

    def shutdown_all(self) -> None:
        with self._lock:
            for name in tuple(self._pools):
                self.stop(name)

    def stop(self, env_name: str) -> bool:
        with self._lock:
            pool = self._pools.pop(env_name, None)
            self._pool_configs.pop(env_name, None)
            self._environments.pop(env_name, None)
            self._specs.pop(env_name, None)
            if pool is None:
                return False
            try:
                pool.close()
            except Exception:
                logger.warning("Failed to close Wetlands pool %r", env_name, exc_info=True)
            return True

    def is_running(self, env_name: str) -> bool:
        with self._lock:
            return env_name in self._pools

    def running_environments(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(sorted(self._pools))
