"""Orchestrator-side Wetlands environment management.

Provides a single shared ``EnvironmentManager`` instance used by both
the execution engine (tool dispatch) and the tool loader (package install).
Call :func:`configure_wetlands` once at the top of your script to set paths;
everything else picks up the same instance automatically.
"""

import inspect
import json
import logging
import os
import threading
from copy import deepcopy
from importlib.metadata import PackageNotFoundError, version as _pkg_version
from pathlib import Path
from typing import Any, cast

from bioimageflow_core.environment import EnvironmentSpec
from bioimageflow.paths import get_wetlands_path

from wetlands._internal.dependency_manager import Dependency, Dependencies, LocalDependency
from wetlands.environment_manager import EnvironmentManager

logger = logging.getLogger("bioimageflow")


def _bioimageflow_core_pin() -> str:
    """Return the package requirement pinning bioimageflow-core.

    Tool environments must run the same ``bioimageflow-core`` API the
    orchestrator was built against, otherwise tools that import newer
    symbols (e.g. ``Connectable``) fail at import time inside the worker.
    """
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
    """Return the Wetlands dependency entry for editable local core installs."""
    return {
        "name": "bioimageflow-core",
        "path": str(project_dir),
        "editable": True,
    }


def _has_bioimageflow_core_dependency(*dependency_lists: list[Any]) -> bool:
    dependencies = [
        dependency
        for dependency_list in dependency_lists
        for dependency in dependency_list
    ]
    for dependency in dependencies:
        name = dependency.get("name") if isinstance(dependency, dict) else dependency
        if isinstance(name, str) and "bioimageflow-core" in name:
            return True
    return False


def _is_local_dependency(dependency: Any) -> bool:
    return isinstance(dependency, dict) and "path" in dependency


def _env_var_is_truthy(name: str) -> bool:
    value = os.environ.get(name, "")
    return value.strip().lower() in {"1", "true", "yes", "on"}

# ── Shared EnvironmentManager singleton ──────────────────────────────

_shared_manager: EnvironmentManager | None = None
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
        Use ``BIOIMAGEFLOW_USE_LOCAL_CORE=1`` to inject the local editable
        ``bioimageflow-core`` checkout into worker environments.
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


def get_shared_environment_manager(**config: Any) -> EnvironmentManager:
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


def _package_sys_path(source_file: Path, module_name: str) -> str | None:
    """Return the import root for a package module, or ``None`` for raw files."""
    if "." not in module_name:
        return None
    top_package = module_name.split(".", 1)[0]
    for parent in [source_file.parent, *source_file.parents]:
        if parent.name != top_package:
            continue
        if (parent / "__init__.py").exists():
            return str(parent.parent)
    return None


def _versioned_package_sys_path(source_file: Path, package: str, version: str) -> str | None:
    """Return the version directory for a tool-store package module."""
    for parent in [source_file.parent, *source_file.parents]:
        if parent.name != package:
            continue
        if parent.parent.name == version and (parent / "__init__.py").exists():
            return str(parent.parent)
    return None


def _find_tool_file(tool_class: type) -> str:
    """Return the absolute file path of the module defining a tool class."""
    worker_module = getattr(tool_class, "_bif_worker_module", None)
    worker_sys_path = getattr(tool_class, "_bif_worker_sys_path", None)
    if worker_module and worker_sys_path:
        return json.dumps({
            "mode": "module",
            "module": worker_module,
            "sys_path": worker_sys_path,
        }, sort_keys=True)

    source_file = Path(inspect.getfile(tool_class)).resolve()
    versioned_module = getattr(tool_class, "_bif_canonical_module", None)
    versioned_package = getattr(tool_class, "_bif_package", None)
    versioned_package_version = getattr(tool_class, "_bif_package_version", None)
    if versioned_module and versioned_package and versioned_package_version:
        versioned_sys_path = _versioned_package_sys_path(
            source_file,
            versioned_package,
            versioned_package_version,
        )
        if versioned_sys_path is not None:
            return json.dumps({
                "mode": "versioned_module",
                "module": tool_class.__module__,
                "package": versioned_package,
                "sys_path": versioned_sys_path,
            }, sort_keys=True)

    try:
        from bioimageflow.workflow import _find_custom_tools_dir
        tools_dir = _find_custom_tools_dir(source_file)
        if tools_dir is not None:
            return json.dumps({
                "mode": "module",
                "module": tool_class.__module__,
                "sys_path": str(tools_dir.parent),
            }, sort_keys=True)
    except Exception:
        pass
    package_sys_path = _package_sys_path(source_file, tool_class.__module__)
    if package_sys_path is not None:
        return json.dumps({
            "mode": "module",
            "module": tool_class.__module__,
            "sys_path": package_sys_path,
        }, sort_keys=True)
    return str(Path(inspect.getfile(tool_class)).resolve())


class WetlandsEnvManager:
    """Manages Wetlands environments for ProcessingTool execution.

    - Creates environments lazily on first use.
    - Caches launched environments by name.
    - Auto-injects ``bioimageflow-core`` into every environment's pip deps.
    - Provides dispatch helpers that route calls through the Wetlands proxy.
    """

    def __init__(
        self,
        wetlands_instance_path: Path | None = None,
        conda_path: str | None = None,
        main_conda_environment_path: str | None = None,
        bioimageflow_core_dependency: Any | None = None,
        use_local_bioimageflow_core: bool | None = None,
        **kwargs: Any,
    ) -> None:
        if wetlands_instance_path is None:
            wetlands_instance_path = get_wetlands_path()
        kwargs.update({
            "wetlands_instance_path": wetlands_instance_path,
            "conda_path": conda_path,
            "main_conda_environment_path": main_conda_environment_path,
        })

        self._manager = get_shared_environment_manager(**kwargs)
        self._envs: dict[str, Any] = {}            # name -> wetlands env
        self._launch_configs: dict[str, tuple[int, Any, float | None]] = {}
        # name -> (max_workers, worker_env, worker_timeout)
        self._worker_file = _find_worker_file()
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
        if use_local_bioimageflow_core:
            project_dir = _local_bioimageflow_core_project()
            if project_dir is None:
                raise RuntimeError(
                    "use_local_bioimageflow_core=True requires an editable "
                    "or source checkout of bioimageflow-core."
                )
            return _bioimageflow_core_editable_dependency(project_dir)
        return _bioimageflow_core_pin()

    def _augment_dependencies(self, dependencies: dict) -> Dependencies:
        """Auto-inject bioimageflow-core into the environment deps."""
        deps = cast(Dependencies, deepcopy(dependencies))
        pip_deps = list(deps.get("pip", []))
        local_deps = list(deps.get("local", []))
        if not _has_bioimageflow_core_dependency(pip_deps, local_deps):
            if _is_local_dependency(self._bioimageflow_core_dependency):
                local_deps.append(
                    cast(LocalDependency, self._bioimageflow_core_dependency)
                )
            else:
                pip_deps.append(
                    cast(str | Dependency, self._bioimageflow_core_dependency)
                )
        deps["pip"] = pip_deps
        if local_deps:
            deps["local"] = local_deps
        return deps

    def get_or_create(
        self,
        env_spec: EnvironmentSpec,
        max_workers: int = 1,
        worker_env: Any = None,
        worker_timeout: float | None = None,
    ) -> Any:
        """Get or create a Wetlands environment.

        Wetlands validates same-name environment recipe reuse. On first creation,
        ``env.launch(max_workers=..., worker_env=..., worker_timeout=...)`` is
        called. Subsequent calls with a different ``max_workers`` or
        ``worker_timeout`` log a warning but do **not** re-launch.

        This method is thread-safe and may be called concurrently.
        """
        augmented_deps = self._augment_dependencies(env_spec.dependencies)

        with self._lock:
            env = self._manager.create(env_spec.name, augmented_deps)
            if env_spec.name in self._envs:
                prev_workers, _, prev_timeout = self._launch_configs.get(
                    env_spec.name, (1, None, None)
                )
                if prev_workers != max_workers:
                    logger.warning(
                        "Environment '%s' already launched with max_workers=%d; "
                        "ignoring new max_workers=%d",
                        env_spec.name, prev_workers, max_workers,
                    )
                if prev_timeout != worker_timeout:
                    logger.warning(
                        "Environment '%s' already launched with worker_timeout=%s; "
                        "ignoring new worker_timeout=%s",
                        env_spec.name, prev_timeout, worker_timeout,
                    )
                return self._envs[env_spec.name]

            logger.info(
                "Creating Wetlands environment '%s' (max_workers=%d, worker_timeout=%s)",
                env_spec.name, max_workers, worker_timeout,
            )
            launch_kwargs: dict[str, Any] = {}
            if max_workers > 1:
                launch_kwargs["max_workers"] = max_workers
            if worker_env is not None:
                launch_kwargs["worker_env"] = worker_env
            if worker_timeout is not None:
                launch_kwargs["worker_timeout"] = worker_timeout
            env.launch(**launch_kwargs)
            self._envs[env_spec.name] = env
            self._launch_configs[env_spec.name] = (max_workers, worker_env, worker_timeout)
            return env

    def submit_process_batch(
        self,
        env_spec: EnvironmentSpec,
        tool_file_path: str,
        tool_class_name: str,
        arguments_dicts: list[dict],
        context_dict: dict,
        max_workers: int = 1,
        worker_env: Any = None,
        worker_timeout: float | None = None,
    ) -> Any:
        """Submit a batch call via ``env.submit()``.  Returns a ``Task``."""
        env = self.get_or_create(
            env_spec, max_workers=max_workers, worker_env=worker_env,
            worker_timeout=worker_timeout,
        )
        return env.submit(
            self._worker_file, "run_process_batch",
            args=(tool_file_path, tool_class_name, arguments_dicts, context_dict),
        )

    def map_process_rows(
        self,
        env_spec: EnvironmentSpec,
        tool_file_path: str,
        tool_class_name: str,
        arguments_dicts: list[dict],
        context_dicts: list[dict],
        max_workers: int = 1,
        worker_env: Any = None,
        worker_timeout: float | None = None,
    ) -> list:
        """Submit per-row calls via ``env.map_tasks()``.  Returns ``list[Task]``."""
        env = self.get_or_create(
            env_spec, max_workers=max_workers, worker_env=worker_env,
            worker_timeout=worker_timeout,
        )
        row_args = [
            (tool_file_path, tool_class_name, d, c)
            for d, c in zip(arguments_dicts, context_dicts)
        ]
        return env.map_tasks(self._worker_file, "run_process_row", row_args)

    def shutdown_all(self) -> None:
        """Shut down all managed Wetlands environments."""
        for name, env in self._envs.items():
            try:
                env.exit()
            except Exception:
                logger.warning("Failed to shut down environment '%s'", name, exc_info=True)
        self._envs.clear()
        self._launch_configs.clear()
