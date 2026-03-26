"""Orchestrator-side Wetlands environment management.

Provides a single shared ``EnvironmentManager`` instance used by both
the execution engine (tool dispatch) and the tool loader (package install).
Call :func:`configure_wetlands` once at the top of your script to set paths;
everything else picks up the same instance automatically.
"""

import inspect
import logging
from pathlib import Path
from typing import Any, cast

from bioimageflow_core.environment import EnvironmentSpec, EnvironmentMismatchError
from bioimageflow.cache import compute_env_hash

from wetlands._internal.dependency_manager import Dependencies
import threading

logger = logging.getLogger("bioimageflow")

# ── Shared EnvironmentManager singleton ──────────────────────────────

_shared_manager: Any = None
_shared_manager_lock = threading.Lock()
_wetlands_config: dict[str, Any] = {}


def configure_wetlands(**config: Any) -> None:
    """Set Wetlands configuration for the entire process.

    Must be called **before** any tool loading or workflow execution.
    Subsequent calls are ignored with a warning if the manager is
    already initialized.

    Common parameters:
        wetlands_instance_path: Path for Wetlands state (logs, pixi).
        conda_path: Path to the pixi or micromamba installation.
        main_conda_environment_path: Main conda env for dep checking.
        debug: Enable debugpy in worker processes.
    """
    global _wetlands_config, _shared_manager
    with _shared_manager_lock:
        if _shared_manager is not None:
            logger.warning(
                "Wetlands already initialized; ignoring configure_wetlands() call. "
                "Call configure_wetlands() before require_tool_packages() or Workflow.compute()."
            )
            return
        _wetlands_config = dict(config)


def get_shared_environment_manager(**config: Any) -> Any:
    """Return the process-wide Wetlands ``EnvironmentManager``.

    On first call, creates the manager using configuration from
    :func:`configure_wetlands` (merged with any *config* kwargs passed
    here).  Subsequent calls return the cached instance.
    """
    global _shared_manager
    if _shared_manager is not None:
        return _shared_manager
    with _shared_manager_lock:
        if _shared_manager is not None:
            return _shared_manager
        from wetlands.environment_manager import EnvironmentManager
        merged = {**_wetlands_config, **config}
        _shared_manager = EnvironmentManager(**merged)
        return _shared_manager


def _reset_shared_manager() -> None:
    """Reset the shared manager (for testing only)."""
    global _shared_manager, _wetlands_config
    _shared_manager = None
    _wetlands_config = {}


def _find_worker_file() -> str:
    """Return the absolute file path to bioimageflow_core/worker.py."""
    import bioimageflow_core
    return str(Path(bioimageflow_core.__file__).parent / "worker.py")


def _find_tool_file(tool_class: type) -> str:
    """Return the absolute file path of the module defining a tool class."""
    return str(Path(inspect.getfile(tool_class)).resolve())


class WetlandsEnvManager:
    """Manages Wetlands environments for ProcessingTool execution.

    - Creates environments lazily on first use.
    - Caches launched environments by name + dependency hash.
    - Auto-injects ``bioimageflow-core`` into every environment's pip deps.
    - Provides dispatch helpers that route calls through the Wetlands proxy.
    """

    def __init__(
        self,
        wetlands_instance_path: Path = Path("wetlands/"),
        conda_path: str | None = None,
        main_conda_environment_path: str | None = None,
        **kwargs: Any,
    ) -> None:
        kwargs.update({
            "wetlands_instance_path": wetlands_instance_path,
            "conda_path": conda_path,
            "main_conda_environment_path": main_conda_environment_path,
        })

        self._manager = get_shared_environment_manager(**kwargs)
        self._envs: dict[str, Any] = {}            # name -> wetlands env
        self._env_hashes: dict[str, str] = {}       # name -> dep hash
        self._worker_proxies: dict[str, Any] = {}   # name -> proxy to worker module
        self._worker_file = _find_worker_file()
        self._lock = threading.RLock()

    def _augment_dependencies(self, dependencies: dict) -> Dependencies:
        """Auto-inject bioimageflow-core into the environment deps."""
        deps = cast(Dependencies, {k: v for k, v in dependencies.items()})
        pip_deps = list(deps.get("pip", []))
        if not any("bioimageflow-core" in d for d in pip_deps):
            pip_deps.append("bioimageflow-core==0.1.1")
        deps["pip"] = pip_deps
        return deps

    def get_or_create(self, env_spec: EnvironmentSpec) -> Any:
        """Get or create a Wetlands environment, validating dependency consistency.

        This method is thread-safe and may be called concurrently.
        """
        augmented_deps = self._augment_dependencies(env_spec.dependencies)
        dep_hash = compute_env_hash(env_spec.dependencies)

        # Fast path: check without lock first
        if env_spec.name in self._envs:
            if self._env_hashes[env_spec.name] != dep_hash:
                raise EnvironmentMismatchError(
                    f"Environment '{env_spec.name}' already created with different deps."
                )
            return self._envs[env_spec.name]

        # Acquire lock for double-checked creation
        with self._lock:
            # Double-check after acquiring lock
            if env_spec.name in self._envs:
                if self._env_hashes[env_spec.name] != dep_hash:
                    raise EnvironmentMismatchError(
                        f"Environment '{env_spec.name}' already created with different deps."
                    )
                return self._envs[env_spec.name]

            logger.info("Creating Wetlands environment '%s'", env_spec.name)
            env = self._manager.create(env_spec.name, augmented_deps)
            env.launch()
            self._envs[env_spec.name] = env
            self._env_hashes[env_spec.name] = dep_hash
            return env

    def _get_worker_proxy(self, env_spec: EnvironmentSpec) -> Any:
        """Get a proxy to bioimageflow_core.worker in the given environment."""
        if env_spec.name not in self._worker_proxies:
            env = self.get_or_create(env_spec)
            self._worker_proxies[env_spec.name] = env.import_module(self._worker_file)
        return self._worker_proxies[env_spec.name]

    def dispatch_process_row(
        self,
        env_spec: EnvironmentSpec,
        tool_file_path: str,
        tool_class_name: str,
        arguments_dict: dict,
    ) -> list[dict]:
        """Dispatch a single-row call through Wetlands."""
        worker = self._get_worker_proxy(env_spec)
        return worker.run_process_row(tool_file_path, tool_class_name, arguments_dict)

    def dispatch_process_batch(
        self,
        env_spec: EnvironmentSpec,
        tool_file_path: str,
        tool_class_name: str,
        arguments_dicts: list[dict],
    ) -> list[list[dict]]:
        """Dispatch a batch call through Wetlands."""
        worker = self._get_worker_proxy(env_spec)
        return worker.run_process_batch(tool_file_path, tool_class_name, arguments_dicts)

    def shutdown_all(self) -> None:
        """Shut down all managed Wetlands environments."""
        for name, env in self._envs.items():
            try:
                env.exit()
            except Exception:
                logger.warning("Failed to shut down environment '%s'", name, exc_info=True)
        self._envs.clear()
        self._worker_proxies.clear()
        self._env_hashes.clear()
