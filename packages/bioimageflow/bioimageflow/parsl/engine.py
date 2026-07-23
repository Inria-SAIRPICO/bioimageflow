"""Attached Parsl configuration and resource lifecycle."""

from __future__ import annotations

import threading
from collections.abc import Generator, Iterator, Mapping
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


class _ParslEngineSteps(Iterator[Any]):
    """Reserve one engine execution before stepped iteration begins."""

    def __init__(
        self,
        engine: "ParslEngine",
        targets: Any,
        workflow: Any,
    ) -> None:
        self._engine = engine
        self._targets = targets
        self._workflow = workflow
        self._iterator: Generator[Any, None, None] | None = None
        self._closed = False
        self._engine._begin_execution()

    def __next__(self) -> Any:
        if self._closed:
            raise StopIteration
        if self._iterator is None:
            self._iterator = self._iterate()
        try:
            return next(self._iterator)
        except StopIteration:
            self._closed = True
            raise
        except BaseException:
            self._closed = True
            raise

    def _iterate(self) -> Generator[Any, None, None]:
        try:
            dfk = self._engine._start_attached_execution()
            yield from self._engine._execute_steps_attached(
                self._targets,
                self._workflow,
                dfk,
            )
        finally:
            self._engine._finish_execution()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._iterator is None:
            self._engine._finish_execution()
            return
        self._iterator.close()


class ParslEngine:
    """Attached Parsl runtime with explicit DFK ownership.

    Construction validates local configuration only.
    DFK acquisition is deferred until execution startup, and processing
    dispatch is supplied through focused hooks implemented by the dispatch
    work package.
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
        self._active_dfk = dfk
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
        self._lifecycle_condition = threading.Condition(threading.RLock())
        self._dfk_lock = threading.RLock()
        self._futures_lock = threading.RLock()
        self._submitted_futures: set[Any] = set()
        self._closed = False
        self._execution_active = False
        self._execution_reserved = False
        self._execution_thread_id: int | None = None
        self._cancel_requested = False

    @property
    def parsl_config(self) -> Any | None:
        return self._parsl_config

    @property
    def dfk(self) -> Any | None:
        with self._dfk_lock:
            return self._active_dfk

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

    @property
    def backend_name(self) -> str:
        return "parsl"

    @property
    def cancel_requested(self) -> bool:
        with self._lifecycle_condition:
            return self._cancel_requested

    def effective_execution(self, workflow: Any) -> str:
        """Resolve the scheduling policy for one root workflow."""
        if self._execution != "workflow":
            return self._execution
        value = getattr(workflow, "execution", None)
        if value not in {"parallel", "sequential"}:
            raise ValueError(
                "The root workflow execution policy must be 'parallel' or "
                "'sequential'."
            )
        return value

    def _ensure_open(self) -> None:
        with self._lifecycle_condition:
            if self._closed:
                raise RuntimeError("This execution engine is closed.")

    def _begin_execution(self) -> None:
        with self._lifecycle_condition:
            if self._closed:
                raise RuntimeError("This execution engine is closed.")
            if self._execution_active:
                if (
                    self._execution_reserved
                    and self._execution_thread_id == threading.get_ident()
                ):
                    self._execution_reserved = False
                    return
                raise RuntimeError(
                    "This ParslEngine already has an active execution."
                )
            self._execution_active = True
            self._execution_reserved = False
            self._execution_thread_id = threading.get_ident()
            self._cancel_requested = False

    def _reserve_execution(self) -> None:
        """Claim the engine before workflow run metadata is created."""
        with self._lifecycle_condition:
            if self._closed:
                raise RuntimeError("This execution engine is closed.")
            if self._execution_active:
                raise RuntimeError(
                    "This ParslEngine already has an active execution."
                )
            self._execution_active = True
            self._execution_reserved = True
            self._execution_thread_id = threading.get_ident()
            self._cancel_requested = False

    def _release_execution_reservation(self) -> None:
        """Release a claim when workflow startup fails before execution."""
        with self._lifecycle_condition:
            if not self._execution_reserved:
                return
            self._execution_active = False
            self._execution_reserved = False
            self._execution_thread_id = None
            self._lifecycle_condition.notify_all()

    def _start_attached_execution(self) -> Any:
        parsl_module = require_parsl()
        with self._dfk_lock:
            if self._active_dfk is not None:
                return self._active_dfk
            kernel_type = getattr(parsl_module, "DataFlowKernel", None)
            if kernel_type is None:
                raise RuntimeError(
                    "The installed Parsl package does not export "
                    "DataFlowKernel."
                )
            self._active_dfk = kernel_type(config=self._parsl_config)
            return self._active_dfk

    def _register_future(self, future: Any) -> None:
        """Register one future owned by the active engine execution."""
        if not callable(getattr(future, "result", None)):
            raise TypeError("A submitted Parsl future must provide result().")
        if not callable(getattr(future, "cancel", None)):
            raise TypeError("A submitted Parsl future must provide cancel().")
        with self._lifecycle_condition:
            if not self._execution_active:
                raise RuntimeError(
                    "Parsl futures may be registered only during execution."
                )
            if self._closed or self._cancel_requested:
                future.cancel()
                raise RuntimeError(
                    "Cannot register a Parsl future after cancellation or close."
                )
            with self._futures_lock:
                self._submitted_futures.add(future)

    def _request_submitted_cancellation(self) -> None:
        with self._lifecycle_condition:
            self._cancel_requested = True
        with self._futures_lock:
            futures = tuple(self._submitted_futures)
        for future in futures:
            try:
                future.cancel()
            except BaseException:
                continue

    def _drain_submitted_futures(self) -> tuple[BaseException, ...]:
        """Wait only for futures registered by this engine."""
        failures: list[BaseException] = []
        while True:
            with self._futures_lock:
                futures = tuple(self._submitted_futures)
            if not futures:
                return tuple(failures)
            for future in futures:
                try:
                    future.result()
                except BaseException as exc:
                    failures.append(exc)
            with self._futures_lock:
                self._submitted_futures.difference_update(futures)

    def _cleanup_owned_dfk(self) -> None:
        if self._resource_lifetime is ResourceLifetime.EXTERNAL:
            return
        with self._dfk_lock:
            dfk = self._active_dfk
            self._active_dfk = None
        if dfk is not None:
            dfk.cleanup()

    def _finish_execution(self) -> None:
        cleanup_error: BaseException | None = None
        try:
            self._drain_submitted_futures()
            with self._lifecycle_condition:
                should_cleanup = (
                    self._resource_lifetime is ResourceLifetime.EXECUTION
                    or self._closed
                )
            if should_cleanup:
                self._cleanup_owned_dfk()
        except BaseException as exc:
            cleanup_error = exc
        finally:
            with self._lifecycle_condition:
                self._execution_active = False
                self._execution_reserved = False
                self._execution_thread_id = None
                self._lifecycle_condition.notify_all()
        if cleanup_error is not None:
            raise cleanup_error

    def execute(self, targets: Any, workflow: Any) -> Any:
        """Execute through the attached DFK dispatch hook."""
        self._begin_execution()
        try:
            dfk = self._start_attached_execution()
            return self._execute_attached(targets, workflow, dfk)
        finally:
            self._finish_execution()

    def _execute_attached(
        self,
        targets: Any,
        workflow: Any,
        dfk: Any,
    ) -> Any:
        del targets, workflow, dfk
        raise NotImplementedError(
            "Parsl workflow dispatch is not configured on this engine."
        )

    def execute_steps(
        self,
        targets: Any,
        workflow: Any,
    ) -> _ParslEngineSteps:
        """Reserve one stepped execution before returning its iterator."""
        return _ParslEngineSteps(self, targets, workflow)

    def _execute_steps_attached(
        self,
        targets: Any,
        workflow: Any,
        dfk: Any,
    ) -> Generator[Any, None, None]:
        del targets, workflow, dfk
        raise NotImplementedError(
            "Parsl stepped dispatch is not configured on this engine."
        )
        yield

    def close(self) -> None:
        """Drain active work and clean resources owned by this engine."""
        with self._lifecycle_condition:
            if self._closed:
                return
            self._closed = True
            execution_active = self._execution_active
            execution_reserved = self._execution_reserved
            called_from_execution = (
                self._execution_thread_id == threading.get_ident()
            )
        self._request_submitted_cancellation()
        if execution_active and not called_from_execution:
            with self._lifecycle_condition:
                while self._execution_active:
                    self._lifecycle_condition.wait()
        elif execution_active and execution_reserved:
            self._release_execution_reservation()
        elif execution_active:
            return
        self._drain_submitted_futures()
        self._cleanup_owned_dfk()

    def __enter__(self) -> "ParslEngine":
        self._ensure_open()
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.close()


__all__ = ["ParslEngine"]
