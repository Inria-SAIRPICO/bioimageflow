"""Local configuration shell for attached Parsl execution."""

from __future__ import annotations

from collections.abc import Generator, Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Any

from bioimageflow.engine import ResourceLifetime
from .optional_dependency import require_parsl
from .types import ExecutorBinding, ParslTaskPolicy


_EXECUTION_POLICIES = frozenset({"workflow", "parallel", "sequential"})
_STORAGE_MODES = frozenset({"shared_fs", "staged"})


def _resource_lifetime_value(
    value: ResourceLifetime | str,
) -> ResourceLifetime:
    try:
        return ResourceLifetime(value)
    except ValueError as exc:
        raise ValueError(
            f"Unknown resource_lifetime {value!r}; expected one of "
            f"{[lifetime.value for lifetime in ResourceLifetime]}."
        ) from exc


def _require_choice(value: Any, *, field: str, allowed: frozenset[str]) -> str:
    if type(value) is not str or value not in allowed:
        raise ValueError(
            f"Unknown {field} {value!r}; expected one of {sorted(allowed)}."
        )
    return value


def _normalize_bindings(
    bindings: Mapping[str, ExecutorBinding],
) -> Mapping[str, ExecutorBinding]:
    if not isinstance(bindings, Mapping):
        raise TypeError("executor_bindings must be a mapping.")
    normalized = dict(bindings)
    if not normalized:
        raise ValueError("executor_bindings must not be empty.")
    for label, binding in normalized.items():
        if type(label) is not str:
            raise TypeError("executor_bindings keys must be strings.")
        if type(binding) is not ExecutorBinding:
            raise TypeError(
                "executor_bindings values must be ExecutorBinding instances."
            )
        if label != binding.label:
            raise ValueError(
                f"Executor binding key {label!r} does not match contained "
                f"label {binding.label!r}."
            )
    return MappingProxyType(normalized)


def _normalize_routes(
    routes: Mapping[str, str] | None,
    *,
    field: str,
    binding_labels: frozenset[str],
) -> Mapping[str, str]:
    if routes is None:
        return MappingProxyType({})
    if not isinstance(routes, Mapping):
        raise TypeError(f"{field} must be a mapping.")
    normalized = dict(routes)
    for route, label in normalized.items():
        if type(route) is not str or not route or route != route.strip():
            raise ValueError(f"{field} keys must be non-empty, trimmed strings.")
        if type(label) is not str or label not in binding_labels:
            raise ValueError(
                f"{field} route {route!r} names unknown executor label "
                f"{label!r}."
            )
    return MappingProxyType(normalized)


def _normalize_shared_runtime_root(value: str | Path | None) -> Path | None:
    if value is None:
        return None
    if not isinstance(value, (str, Path)):
        raise TypeError("shared_runtime_root must be a string, Path, or None.")
    if isinstance(value, str) and (not value or value != value.strip()):
        raise ValueError(
            "shared_runtime_root must be a non-empty, trimmed path."
        )
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    return path.resolve(strict=False)


class ParslEngine:
    """Validate and retain attached Parsl runtime configuration.

    The constructor performs no external Parsl import and acquires no runtime
    resources. Execution is supplied by the later dispatch and lifecycle work
    packages.
    """

    def __init__(
        self,
        *,
        parsl_config: Any | None = None,
        dfk: Any | None = None,
        executor_bindings: Mapping[str, ExecutorBinding],
        node_routes: Mapping[str, str] | None = None,
        environment_routes: Mapping[str, str] | None = None,
        shared_runtime_root: str | Path | None = None,
        execution: str = "workflow",
        storage_mode: str = "shared_fs",
        task_policy: ParslTaskPolicy | None = None,
        resource_lifetime: ResourceLifetime | str = "execution",
    ) -> None:
        if (parsl_config is None) == (dfk is None):
            raise ValueError("Exactly one of parsl_config and dfk is required.")

        lifetime = _resource_lifetime_value(resource_lifetime)
        if dfk is not None and lifetime is not ResourceLifetime.EXTERNAL:
            raise ValueError(
                "An injected dfk requires resource_lifetime='external'."
            )
        if dfk is None and lifetime is ResourceLifetime.EXTERNAL:
            raise ValueError(
                "resource_lifetime='external' requires an injected dfk."
            )

        selected_storage_mode = _require_choice(
            storage_mode,
            field="storage_mode",
            allowed=_STORAGE_MODES,
        )
        if selected_storage_mode == "staged":
            raise ValueError(
                "storage_mode='staged' is unavailable in Parsl Phase 1a."
            )

        if task_policy is not None and type(task_policy) is not ParslTaskPolicy:
            raise TypeError("task_policy must be ParslTaskPolicy or None.")

        bindings = _normalize_bindings(executor_bindings)
        binding_labels = frozenset(bindings)
        self._parsl_config = parsl_config
        self._dfk = dfk
        self._executor_bindings = bindings
        self._node_routes = _normalize_routes(
            node_routes,
            field="node_routes",
            binding_labels=binding_labels,
        )
        self._environment_routes = _normalize_routes(
            environment_routes,
            field="environment_routes",
            binding_labels=binding_labels,
        )
        self._shared_runtime_root = _normalize_shared_runtime_root(
            shared_runtime_root
        )
        self._execution = _require_choice(
            execution,
            field="execution",
            allowed=_EXECUTION_POLICIES,
        )
        self._storage_mode = selected_storage_mode
        self._task_policy = task_policy or ParslTaskPolicy()
        self._resource_lifetime = lifetime
        self._closed = False

    @property
    def parsl_config(self) -> Any | None:
        return self._parsl_config

    @property
    def dfk(self) -> Any | None:
        return self._dfk

    @property
    def executor_bindings(self) -> Mapping[str, ExecutorBinding]:
        return self._executor_bindings

    @property
    def node_routes(self) -> Mapping[str, str]:
        return self._node_routes

    @property
    def environment_routes(self) -> Mapping[str, str]:
        return self._environment_routes

    @property
    def shared_runtime_root(self) -> Path | None:
        return self._shared_runtime_root

    @property
    def execution(self) -> str:
        return self._execution

    @property
    def storage_mode(self) -> str:
        return self._storage_mode

    @property
    def task_policy(self) -> ParslTaskPolicy:
        return self._task_policy

    @property
    def resource_lifetime(self) -> ResourceLifetime:
        return self._resource_lifetime

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("This execution engine is closed.")

    def execute(self, targets: Any, workflow: Any) -> Any:
        """Enter the lazy optional-dependency boundary for execution."""
        self._ensure_open()
        require_parsl()
        raise NotImplementedError(
            "Parsl workflow dispatch is implemented by the later execution "
            "work package."
        )

    def execute_steps(
        self,
        targets: Any,
        workflow: Any,
    ) -> Generator[Any, None, None]:
        """Enter the lazy optional-dependency boundary for stepped execution."""
        self._ensure_open()
        require_parsl()
        raise NotImplementedError(
            "Parsl stepped dispatch is implemented by the later execution "
            "work package."
        )
        yield

    def close(self) -> None:
        """Close this local shell; no external resource has been acquired."""
        self._closed = True

    def __enter__(self) -> "ParslEngine":
        self._ensure_open()
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.close()


__all__ = ["ParslEngine"]
