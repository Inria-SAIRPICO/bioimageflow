"""Isolated, non-allocating validation for managed Parsl factories."""

from __future__ import annotations

import importlib
import io
import multiprocessing
import os
import shutil
import signal
import tempfile
from collections.abc import Mapping
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from dataclasses import dataclass
from pathlib import PurePosixPath
from types import MappingProxyType
from typing import Any, ClassVar

from .factory import ParslFactoryRuntime


_MAX_SECRET_BYTES = 64 * 1024
_MAX_TOTAL_SECRET_BYTES = 256 * 1024
_MAX_CAPTURED_OUTPUT_BYTES = 256 * 1024


class _BoundedTextBuffer(io.StringIO):
    """Discard output after a fixed UTF-8 byte limit."""

    def __init__(self, limit: int) -> None:
        super().__init__()
        self._limit = limit
        self._size = 0

    def write(self, value: str) -> int:
        encoded = value.encode("utf-8", errors="replace")
        available = max(0, self._limit - self._size)
        if available:
            retained = encoded[:available].decode("utf-8", errors="ignore")
            super().write(retained)
            self._size += len(retained.encode("utf-8"))
        return len(value)


@dataclass(frozen=True, slots=True)
class FactoryValidationDiagnostic:
    """One stable public factory-validation failure."""

    category: str
    message: str
    field: str | None = None

    def to_dict(self) -> dict[str, str | None]:
        return {
            "category": self.category,
            "message": self.message,
            "field": self.field,
        }

    @classmethod
    def from_dict(cls, value: Any) -> "FactoryValidationDiagnostic":
        if type(value) is not dict or set(value) != {"category", "message", "field"}:
            raise ValueError("Invalid FactoryValidationDiagnostic payload.")
        return cls(value["category"], value["message"], value["field"])


@dataclass(frozen=True, slots=True)
class ManagedFactoryValidationReport:
    """Sanitized facts produced after all live Parsl values are discarded."""

    SCHEMA: ClassVar[str] = "bioimageflow.managed_factory_validation.v1"

    valid: bool
    executor_labels: tuple[str, ...]
    retries: int | None
    executor_bindings: Mapping[str, Any]
    provider_evidence: tuple[Mapping[str, Any], ...] = ()
    diagnostics: tuple[FactoryValidationDiagnostic, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "executor_bindings",
            MappingProxyType(
                {key: dict(value) for key, value in self.executor_bindings.items()}
            ),
        )
        object.__setattr__(
            self,
            "provider_evidence",
            tuple(MappingProxyType(dict(item)) for item in self.provider_evidence),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "valid": self.valid,
            "executor_labels": list(self.executor_labels),
            "retries": self.retries,
            "executor_bindings": {
                label: dict(binding)
                for label, binding in self.executor_bindings.items()
            },
            "provider_evidence": [dict(item) for item in self.provider_evidence],
            "diagnostics": [item.to_dict() for item in self.diagnostics],
        }

    @classmethod
    def from_dict(cls, value: Any) -> "ManagedFactoryValidationReport":
        expected = {
            "schema",
            "valid",
            "executor_labels",
            "retries",
            "executor_bindings",
            "provider_evidence",
            "diagnostics",
        }
        if (
            type(value) is not dict
            or set(value) != expected
            or value["schema"] != cls.SCHEMA
        ):
            raise ValueError("Invalid ManagedFactoryValidationReport payload.")
        return cls(
            valid=value["valid"],
            executor_labels=tuple(value["executor_labels"]),
            retries=value["retries"],
            executor_bindings=value["executor_bindings"],
            provider_evidence=tuple(value["provider_evidence"]),
            diagnostics=tuple(
                FactoryValidationDiagnostic.from_dict(item)
                for item in value["diagnostics"]
            ),
        )


def _runtime_payload(runtime: ParslFactoryRuntime) -> dict[str, Any]:
    return {
        "deployment_root": str(runtime.deployment_root),
        "deployment_id": runtime.deployment_id,
        "worker_init": runtime.worker_init,
        "environment_name": runtime.environment_name,
        "environment_identity": runtime.environment_identity,
        "core_requirement": runtime.core_requirement,
        "storage_mode": runtime.storage_mode,
        "tool_origin_modes": runtime.tool_origin_modes,
    }


def _runtime_from_payload(payload: Mapping[str, Any]) -> ParslFactoryRuntime:
    return ParslFactoryRuntime(
        deployment_root=PurePosixPath(payload["deployment_root"]),
        deployment_id=payload["deployment_id"],
        worker_init=payload["worker_init"],
        environment_name=payload["environment_name"],
        environment_identity=payload["environment_identity"],
        core_requirement=payload["core_requirement"],
        storage_mode=payload["storage_mode"],
        tool_origin_modes=tuple(payload["tool_origin_modes"]),
    )


def _factory(reference: str) -> Any:
    module_name, separator, name = reference.partition(":")
    if separator != ":" or not module_name or not name or not name.isidentifier():
        raise ValueError("Factory must use 'module:identifier' syntax.")
    value = getattr(importlib.import_module(module_name), name)
    if not callable(value):
        raise TypeError("The selected factory is not callable.")
    return value


def _minimal_environment(private_root: str) -> None:
    retained = {
        name: os.environ[name]
        for name in ("PATH", "LANG", "LC_ALL", "PYTHONPATH")
        if name in os.environ
    }
    os.environ.clear()
    os.environ.update(retained)
    os.environ.update({"HOME": private_root, "TMPDIR": private_root})


def _forbidden(operation: str) -> Any:
    def reject(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError(
            f"Parsl {operation} is forbidden during managed factory validation."
        )

    return reject


def _validation_patches(stack: ExitStack) -> None:
    from unittest.mock import patch

    import parsl
    from parsl.dataflow.dflow import DataFlowKernel
    from parsl.executors import HighThroughputExecutor
    from parsl.providers import LSFProvider, PBSProProvider, SlurmProvider, TorqueProvider

    stack.enter_context(patch.object(parsl, "load", _forbidden("load")))
    stack.enter_context(
        patch.object(DataFlowKernel, "__init__", _forbidden("DataFlowKernel creation"))
    )
    stack.enter_context(
        patch.object(HighThroughputExecutor, "start", _forbidden("executor start"))
    )
    # Parsl probes sacct in SlurmProvider.__init__.  Validation replaces that
    # public command helper so constructing the trusted configuration performs
    # no scheduler command at all.
    stack.enter_context(
        patch.object(SlurmProvider, "execute_wait", return_value=(1, "", ""))
    )
    for provider_type in (SlurmProvider, PBSProProvider, TorqueProvider, LSFProvider):
        stack.enter_context(
            patch.object(provider_type, "submit", _forbidden("provider submission"))
        )


def _managed_binding(binding: Any, runtime: ParslFactoryRuntime) -> bool:
    return any(
        environment.name == runtime.environment_name
        for environment in binding.environments
    )


def _validate_result(
    result: Any,
    *,
    runtime: ParslFactoryRuntime,
    orchestrator_scheduler: str,
) -> ManagedFactoryValidationReport:
    from parsl import Config

    from .factory import normalize_factory_result
    from .providers import ManagedProviderError, validate_managed_provider

    normalized = normalize_factory_result(result)
    if not isinstance(normalized.config, Config):
        raise TypeError("ParslFactoryResult.config must be a live parsl.Config value.")
    raw_retries = normalized.config.retries
    retries = raw_retries if type(raw_retries) is int else None
    if retries != 0:
        return _failure(
            "parsl-retries-enabled",
            "Parsl Config.retries must be exactly 0.",
            field="retries",
            retries=retries,
        )
    executors = tuple(normalized.config.executors)
    raw_labels = tuple(getattr(executor, "label", None) for executor in executors)
    if any(
        type(label) is not str or not label for label in raw_labels
    ) or len(raw_labels) != len(set(raw_labels)):
        return _failure(
            "executor-label-mismatch",
            "Parsl executor labels must be unique non-empty strings.",
            field="executor_bindings",
            retries=retries,
        )
    labels = tuple(str(label) for label in raw_labels)
    label_set = set(labels)
    if label_set != set(normalized.executor_bindings):
        return _failure(
            "executor-label-mismatch",
            "Parsl executor labels and executor binding labels differ.",
            field="executor_bindings",
            retries=retries,
        )
    bindings = {
        label: normalized.executor_bindings[label].to_dict()
        for label in sorted(label_set)
    }
    evidence: list[Mapping[str, Any]] = []
    for executor in executors:
        binding = normalized.executor_bindings[executor.label]
        if binding.capabilities.storage_modes != ("shared_fs",):
            return _failure(
                "route-incompatible",
                "Managed cluster validation requires shared_fs executor storage.",
                field="executor_bindings",
                retries=retries,
            )
        if not _managed_binding(binding, runtime):
            continue
        try:
            provider = validate_managed_provider(
                executor,
                worker_init=runtime.worker_init,
                orchestrator_scheduler=orchestrator_scheduler,
            )
        except ManagedProviderError as exc:
            return _failure(
                exc.category,
                str(exc),
                field="executor_bindings",
                retries=retries,
            )
        evidence.append(provider.to_dict())
    return ManagedFactoryValidationReport(
        valid=True,
        executor_labels=tuple(sorted(label_set)),
        retries=retries,
        executor_bindings=bindings,
        provider_evidence=tuple(sorted(evidence, key=lambda item: item["label"])),
    )


def _failure(
    category: str,
    message: str,
    *,
    field: str | None = None,
    retries: int | None = None,
) -> ManagedFactoryValidationReport:
    return ManagedFactoryValidationReport(
        valid=False,
        executor_labels=(),
        retries=retries,
        executor_bindings={},
        diagnostics=(FactoryValidationDiagnostic(category, message, field),),
    )


def _worker(
    report_connection: Any,
    secret_connection: Any,
    factory_reference: str,
    runtime_payload: Mapping[str, Any],
    kwargs: Mapping[str, Any],
    secret_refs: Mapping[str, str],
    orchestrator_scheduler: str,
    private_root: str,
) -> None:
    try:
        os.setsid()
    except (AttributeError, OSError):
        pass
    try:
        os.chdir(private_root)
        _minimal_environment(private_root)
        secrets = secret_connection.recv()
        secret_connection.close()
        has_secrets = bool(secrets)
        output = _BoundedTextBuffer(_MAX_CAPTURED_OUTPUT_BYTES)
        try:
            arguments = dict(kwargs)
            for argument, reference in secret_refs.items():
                arguments[argument] = secrets[reference]
            with redirect_stdout(output), redirect_stderr(output), ExitStack() as stack:
                _validation_patches(stack)
                result = _factory(factory_reference)(
                    _runtime_from_payload(runtime_payload), **arguments
                )
                report = _validate_result(
                    result,
                    runtime=_runtime_from_payload(runtime_payload),
                    orchestrator_scheduler=orchestrator_scheduler,
                )
        except BaseException as exc:
            # Arbitrary trusted output and exception text are deliberately not
            # returned when resolved secrets were present.
            message = (
                "The trusted Parsl factory failed while resolved secrets were present."
                if has_secrets
                else (str(exc) or type(exc).__name__)[:4096]
            )
            report = _failure("parsl-factory-failed", message, field="factory")
        finally:
            secrets.clear()
        report_connection.send(report.to_dict())
        report_connection.close()
    finally:
        # The parent owns removal too, including after timeout termination.
        os.chdir("/")


def _resolve_secrets(
    secret_refs: Mapping[str, str],
    secret_values: Mapping[str, str] | None,
) -> tuple[dict[str, str] | None, ManagedFactoryValidationReport | None]:
    resolved: dict[str, str] = {}
    total = 0
    source = os.environ if secret_values is None else secret_values
    for reference in secret_refs.values():
        if reference not in source:
            return None, _failure(
                "secret-reference-missing",
                f"Required secret reference {reference!r} is unavailable.",
                field="secret_refs",
            )
        value = source[reference]
        if type(value) is not str or "\0" in value:
            return None, _failure(
                "parsl-factory-failed",
                f"Secret reference {reference!r} has an invalid value.",
                field="secret_refs",
            )
        size = len(value.encode("utf-8"))
        total += size
        if size > _MAX_SECRET_BYTES or total > _MAX_TOTAL_SECRET_BYTES:
            return None, _failure(
                "resource-limit-exceeded",
                "Resolved factory secrets exceed the validation handoff limit.",
                field="secret_refs",
            )
        resolved[reference] = value
    return resolved, None


def validate_managed_factory(
    factory: str,
    *,
    runtime: ParslFactoryRuntime,
    orchestrator_scheduler: str,
    kwargs: Mapping[str, Any] | None = None,
    secret_refs: Mapping[str, str] | None = None,
    secret_values: Mapping[str, str] | None = None,
    timeout: float = 30.0,
) -> ManagedFactoryValidationReport:
    """Invoke a managed factory in a bounded child and return sanitized facts."""
    if type(runtime) is not ParslFactoryRuntime:
        raise TypeError("runtime must be ParslFactoryRuntime.")
    if type(factory) is not str or not factory:
        raise ValueError("factory must be a non-empty module reference.")
    if type(timeout) not in {int, float} or timeout <= 0:
        raise ValueError("timeout must be positive.")
    arguments = dict(kwargs or {})
    references = dict(secret_refs or {})
    if set(arguments).intersection(references):
        raise ValueError("Factory arguments cannot occur in kwargs and secret_refs.")
    resolved, failure = _resolve_secrets(references, secret_values)
    if failure is not None:
        return failure
    assert resolved is not None
    context = multiprocessing.get_context("spawn")
    report_parent, report_child = context.Pipe(duplex=False)
    secret_child, secret_parent = context.Pipe(duplex=False)
    private_root = tempfile.mkdtemp(prefix="bioimageflow-factory-")
    process = context.Process(
        target=_worker,
        args=(
            report_child,
            secret_child,
            factory,
            _runtime_payload(runtime),
            arguments,
            references,
            orchestrator_scheduler,
            private_root,
        ),
        name="bioimageflow-managed-factory-validation",
    )
    process.start()
    report_child.close()
    secret_child.close()
    try:
        try:
            secret_parent.send(resolved)
        except (BrokenPipeError, EOFError, OSError):
            return _failure(
                "parsl-factory-failed",
                "The managed Parsl factory child exited before validation.",
                field="factory",
            )
        resolved.clear()
        secret_parent.close()
        if not report_parent.poll(timeout):
            _terminate_process(process)
            return _failure(
                "parsl-factory-failed",
                "Managed Parsl factory validation timed out.",
                field="factory",
            )
        try:
            return ManagedFactoryValidationReport.from_dict(report_parent.recv())
        except (EOFError, OSError, TypeError, ValueError):
            return _failure(
                "parsl-factory-failed",
                "The managed Parsl factory child returned no valid report.",
                field="factory",
            )
    finally:
        resolved.clear()
        report_parent.close()
        if not secret_parent.closed:
            secret_parent.close()
        process.join(5)
        if process.is_alive():
            _terminate_process(process)
        shutil.rmtree(private_root, ignore_errors=True)


def _terminate_process(process: Any) -> None:
    if process.pid is not None:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except (AttributeError, ProcessLookupError, PermissionError):
            process.terminate()
    process.join(5)
    if process.is_alive():
        process.kill()
        process.join(5)


__all__ = [
    "FactoryValidationDiagnostic",
    "ManagedFactoryValidationReport",
    "validate_managed_factory",
]
