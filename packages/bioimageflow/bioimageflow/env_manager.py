"""Orchestrator-side Wetlands 2 environment management."""

from __future__ import annotations

import logging
import re
import threading
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Literal

from bioimageflow_core.environment import EnvironmentSpec as BioImageFlowEnvironmentSpec
from bioimageflow._core_dependency import (
    CoreRequirementConflictError,
    _bioimageflow_core_editable_dependency,
    _bioimageflow_core_pin,
    _bioimageflow_core_source_from_environment,
    _env_var_is_truthy,
    _is_core_dependency,
    _is_local_dependency,
    _local_bioimageflow_core_project,
    _validated_bioimageflow_core_project,
    core_requirement_conflict,
)
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
        bioimageflow_core_source: str | Path | None = None,
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
            if (
                bioimageflow_core_source is not None
                and use_local_bioimageflow_core is False
            ):
                raise ValueError(
                    "bioimageflow_core_source cannot be combined with "
                    "use_local_bioimageflow_core=False."
                )
            if use_local_bioimageflow_core is False:
                configured_source = None
            elif bioimageflow_core_source is not None:
                configured_source = _validated_bioimageflow_core_project(
                    bioimageflow_core_source,
                    setting="bioimageflow_core_source",
                )
            else:
                configured_source = _bioimageflow_core_source_from_environment()
            if use_local_bioimageflow_core is None:
                use_local_bioimageflow_core = _env_var_is_truthy(
                    "BIOIMAGEFLOW_USE_LOCAL_CORE"
                )
            self._bioimageflow_core_dependency = self._default_core_dependency(
                use_local_bioimageflow_core=use_local_bioimageflow_core,
                bioimageflow_core_source=configured_source,
            )

    @staticmethod
    def _default_core_dependency(
        *,
        use_local_bioimageflow_core: bool,
        bioimageflow_core_source: Path | None = None,
    ) -> Any:
        if bioimageflow_core_source is not None:
            return _bioimageflow_core_editable_dependency(bioimageflow_core_source)
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
