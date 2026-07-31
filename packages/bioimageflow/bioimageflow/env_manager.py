"""Orchestrator-side Wetlands 2 environment management."""

from __future__ import annotations

import logging
import os
import re
import threading
import urllib.parse
import urllib.request
from collections.abc import Mapping
from copy import deepcopy
from importlib.metadata import PackageNotFoundError, version as _pkg_version
from pathlib import Path
from typing import Any

from bioimageflow_core.environment import EnvironmentSpec as BioImageFlowEnvironmentSpec
from bioimageflow.paths import get_wetlands_path
from wetlands import (
    EnvironmentManager,
    EnvironmentSpec,
    LocalPackage,
    ManagedEnvironment,
    WorkerPool,
)

logger = logging.getLogger("bioimageflow")

_WORKER_TARGET = "bioimageflow_core.worker:execute_processing_task"
_LOCAL_PYPI_REFERENCE = re.compile(
    r"^\s*(?P<name>[A-Za-z0-9_.-]+)"
    r"(?:\[(?P<extras>[A-Za-z0-9_.,-]+)\])?"
    r"\s*@\s*(?P<url>file://\S+)\s*$"
)


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


def _has_bioimageflow_core_dependency(*dependency_lists: list[Any]) -> bool:
    return any(
        (_dependency_name(dependency) or "").replace("_", "-").lower()
        == "bioimageflow-core"
        for dependency_list in dependency_lists
        for dependency in dependency_list
    )


def _is_local_dependency(dependency: Any) -> bool:
    return isinstance(dependency, dict) and "path" in dependency


def _env_var_is_truthy(name: str) -> bool:
    value = os.environ.get(name, "")
    return value.strip().lower() in {"1", "true", "yes", "on"}


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
    channels: list[str],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    translated: list[str] = []
    ordered_channels = list(channels)
    for value in values:
        if not isinstance(value, str):
            raise TypeError(f"Wetlands 2 Conda dependencies must be strings, got {value!r}.")
        if "::" in value:
            channel, value = value.split("::", 1)
            if channel and channel not in ordered_channels:
                ordered_channels.append(channel)
        translated.append(value)
    if not ordered_channels:
        ordered_channels.append("conda-forge")
    return tuple(translated), tuple(ordered_channels)


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
        pip_deps = list(deps.get("pip", []))
        local_deps = list(deps.get("local", []))
        if not _has_bioimageflow_core_dependency(pip_deps, local_deps):
            if _is_local_dependency(self._bioimageflow_core_dependency):
                local_deps.append(deepcopy(self._bioimageflow_core_dependency))
            else:
                pip_deps.append(self._bioimageflow_core_dependency)
        deps["pip"] = pip_deps
        if local_deps:
            deps["local"] = local_deps
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
        raw_channels = list(dependencies.get("channels", []))
        if any(not isinstance(channel, str) for channel in raw_channels):
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

    def get_or_create(
        self,
        env_spec: BioImageFlowEnvironmentSpec,
        max_workers: int = 1,
        worker_timeout: float | None = None,
    ) -> WorkerPool:
        """Provision an environment and return its cached Wetlands 2 pool."""
        wetlands_spec = self._to_wetlands_spec(env_spec)
        config = (max_workers, worker_timeout)
        with self._lock:
            existing = self._pools.get(env_spec.name)
            if existing is not None:
                if self._specs[env_spec.name] != wetlands_spec:
                    raise ValueError(
                        f"Environment {env_spec.name!r} was already provisioned "
                        "with a different recipe."
                    )
                if self._pool_configs[env_spec.name] != config:
                    raise ValueError(
                        f"Environment {env_spec.name!r} already has a pool with "
                        f"workers={self._pool_configs[env_spec.name][0]} and "
                        f"worker_timeout={self._pool_configs[env_spec.name][1]}."
                    )
                return existing
            environment = self._manager.provision(
                env_spec.name,
                wetlands_spec,
            ).wait_for()
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
