"""Public engine-neutral integration contracts for execution frontends."""

from __future__ import annotations

import importlib.util
import multiprocessing
import os
import re
import traceback
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, ClassVar

from .launcher.types import ParslConfigRef

_SECRET_NAME = re.compile(r"(?:credential|password|secret|token|api[_-]?key)", re.I)


def _sanitize_text(
    value: str | None,
    *,
    additional_secrets: Collection[str] = (),
) -> str | None:
    if value is None:
        return None
    sanitized = str(value)
    for name, secret in os.environ.items():
        if secret and _SECRET_NAME.search(name):
            sanitized = sanitized.replace(secret, "<redacted>")
    for secret in additional_secrets:
        if secret:
            sanitized = sanitized.replace(secret, "<redacted>")
    return sanitized


@dataclass(frozen=True, slots=True)
class IntegrationDiagnostic:
    """Stable sanitized diagnostic returned by preflight operations."""

    code: str
    message: str
    field: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "field": self.field}

    @classmethod
    def from_dict(cls, value: Any) -> "IntegrationDiagnostic":
        if not isinstance(value, dict) or set(value) != {"code", "message", "field"}:
            raise ValueError("Invalid IntegrationDiagnostic payload.")
        return cls(str(value["code"]), str(value["message"]), value["field"])


@dataclass(frozen=True, slots=True)
class ParslConfigValidationReport:
    """Non-allocating result of resolving a trusted Parsl Config reference."""

    SCHEMA: ClassVar[str] = "bioimageflow.parsl_config_validation.v1"

    valid: bool
    executor_labels: tuple[str, ...]
    retries: int | None
    diagnostics: tuple[IntegrationDiagnostic, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "valid": self.valid,
            "executor_labels": list(self.executor_labels),
            "retries": self.retries,
            "diagnostics": [item.to_dict() for item in self.diagnostics],
        }

    @classmethod
    def from_dict(cls, value: Any) -> "ParslConfigValidationReport":
        if not isinstance(value, dict) or value.get("schema") != cls.SCHEMA:
            raise ValueError("Invalid ParslConfigValidationReport payload.")
        return cls(
            valid=value["valid"],
            executor_labels=tuple(value["executor_labels"]),
            retries=value["retries"],
            diagnostics=tuple(
                IntegrationDiagnostic.from_dict(item)
                for item in value["diagnostics"]
            ),
        )


def _config_validation_worker(
    connection: Any,
    reference_payload: dict[str, Any],
    binding_labels: tuple[str, ...],
    trusted_factories: tuple[str, ...],
) -> None:
    diagnostics: list[IntegrationDiagnostic] = []
    retries: int | None = None
    labels: tuple[str, ...] = ()
    secret_refs = reference_payload.get("secret_refs") or {}
    resolved_secrets = tuple(
        value
        for reference in secret_refs.values()
        if (value := os.environ.get(reference))
    )
    try:
        from .launcher.configuration import (
            build_parsl_config,
            inspect_parsl_config,
        )

        config = build_parsl_config(
            ParslConfigRef.from_dict(reference_payload),
            trusted_factories=trusted_factories,
        )
        retries, labels, issues = inspect_parsl_config(
            config,
            binding_labels=binding_labels,
        )
        diagnostics.extend(
            IntegrationDiagnostic(code, message, field)
            for code, message, field in issues
        )
    except Exception as exc:
        details = getattr(exc, "details", {})
        secret_ref = details.get("secret_ref") if isinstance(details, dict) else None
        if secret_ref is not None:
            diagnostics.append(
                IntegrationDiagnostic(
                    "missing-secret-reference",
                    f"Required secret reference {secret_ref!r} is unavailable.",
                    "secret_refs",
                )
            )
        else:
            diagnostics.append(
                IntegrationDiagnostic(
                    "config-resolution",
                    _sanitize_text(
                        str(exc),
                        additional_secrets=resolved_secrets,
                    )
                    or type(exc).__name__,
                    "factory",
                )
            )
    report = ParslConfigValidationReport(
        valid=not diagnostics,
        executor_labels=labels,
        retries=retries,
        diagnostics=tuple(diagnostics),
    )
    connection.send(report.to_dict())
    connection.close()


def validate_parsl_config_ref(
    reference: ParslConfigRef,
    *,
    executor_bindings: Mapping[str, Any],
    trusted_factories: Collection[str],
    timeout: float = 30.0,
) -> ParslConfigValidationReport:
    """Resolve a trusted config in an isolated process without creating a DFK."""
    if type(reference) is not ParslConfigRef:
        raise TypeError("reference must be ParslConfigRef.")
    from .parsl.types import ExecutorBinding

    bindings = dict(executor_bindings)
    if not bindings or any(
        type(label) is not str or type(binding) is not ExecutorBinding
        for label, binding in bindings.items()
    ):
        raise TypeError("executor_bindings must contain ExecutorBinding values.")
    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe(duplex=False)
    process = context.Process(
        target=_config_validation_worker,
        args=(
            child,
            reference.to_dict(),
            tuple(sorted(bindings)),
            tuple(trusted_factories),
        ),
        name="bioimageflow-parsl-config-validation",
    )
    process.start()
    child.close()
    try:
        if not parent.poll(timeout):
            process.terminate()
            process.join(5)
            return ParslConfigValidationReport(
                valid=False,
                executor_labels=(),
                retries=None,
                diagnostics=(
                    IntegrationDiagnostic(
                        "validation-timeout",
                        "Parsl configuration validation timed out.",
                    ),
                ),
            )
        return ParslConfigValidationReport.from_dict(parent.recv())
    finally:
        parent.close()
        process.join(5)
        if process.is_alive():
            process.terminate()
            process.join(5)


@dataclass(frozen=True, slots=True)
class NodeFailureDiagnostic:
    """Structured, serializable failure for one scoped node attempt."""

    SCHEMA: ClassVar[str] = "bioimageflow.node_failure.v1"

    scoped_node_path: str
    category: str
    exception_type: str
    message: str
    traceback: str | None = None
    attempt_id: str | None = None
    retry_status: str = "terminal"
    terminal: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "scoped_node_path": self.scoped_node_path,
            "category": self.category,
            "exception_type": self.exception_type,
            "message": self.message,
            "traceback": self.traceback,
            "attempt_id": self.attempt_id,
            "retry_status": self.retry_status,
            "terminal": self.terminal,
        }

    @classmethod
    def from_dict(cls, value: Any) -> "NodeFailureDiagnostic":
        if not isinstance(value, dict) or value.get("schema") != cls.SCHEMA:
            raise ValueError("Invalid NodeFailureDiagnostic payload.")
        return cls(**{key: item for key, item in value.items() if key != "schema"})

    @classmethod
    def from_exception(
        cls,
        scoped_node_path: str,
        exception: BaseException,
        *,
        attempt_id: str | None = None,
        category: str = "execution",
    ) -> "NodeFailureDiagnostic":
        failure = getattr(exception, "failure", None)
        if failure is not None:
            remote = getattr(failure, "remote_exception", None)
            failure_category = getattr(getattr(failure, "category", None), "value", None)
            return cls(
                scoped_node_path=scoped_node_path,
                category=failure_category or category,
                exception_type=(
                    getattr(remote, "type_name", None) or type(exception).__name__
                ),
                message=_sanitize_text(
                    getattr(remote, "message", None)
                    or getattr(failure, "message", None)
                    or str(exception)
                )
                or "",
                traceback=_sanitize_text(
                    getattr(remote, "traceback", None)
                    or getattr(failure, "traceback", None)
                ),
                attempt_id=attempt_id or getattr(failure, "task_id", None),
            )
        return cls(
            scoped_node_path=scoped_node_path,
            category=category,
            exception_type=type(exception).__name__,
            message=_sanitize_text(str(exception)) or "",
            traceback=_sanitize_text("".join(
                traceback.format_exception(
                    type(exception),
                    exception,
                    exception.__traceback__,
                )
            )),
            attempt_id=attempt_id,
        )


@dataclass(frozen=True, slots=True)
class CapabilityStatus:
    supported: bool
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"supported": self.supported, "reason": self.reason}

    @classmethod
    def from_dict(cls, value: Any) -> "CapabilityStatus":
        if not isinstance(value, dict) or set(value) != {"supported", "reason"}:
            raise ValueError("Invalid CapabilityStatus payload.")
        return cls(supported=value["supported"], reason=value["reason"])


@dataclass(frozen=True, slots=True)
class ExecutionCapabilityReport:
    """Compact optional-dependency and execution-contract capability report."""

    SCHEMA: ClassVar[str] = "bioimageflow.execution_capabilities.v1"
    capabilities: Mapping[str, CapabilityStatus]

    def __post_init__(self) -> None:
        if not isinstance(self.capabilities, Mapping) or any(
            type(name) is not str or type(status) is not CapabilityStatus
            for name, status in self.capabilities.items()
        ):
            raise TypeError("capabilities must map names to CapabilityStatus values.")
        object.__setattr__(
            self,
            "capabilities",
            MappingProxyType(dict(self.capabilities)),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "capabilities": {
                key: value.to_dict() for key, value in self.capabilities.items()
            },
        }

    @classmethod
    def from_dict(cls, value: Any) -> "ExecutionCapabilityReport":
        if (
            not isinstance(value, dict)
            or set(value) != {"schema", "capabilities"}
            or value["schema"] != cls.SCHEMA
            or not isinstance(value["capabilities"], dict)
        ):
            raise ValueError("Invalid ExecutionCapabilityReport payload.")
        return cls(
            capabilities={
                key: CapabilityStatus.from_dict(item)
                for key, item in value["capabilities"].items()
            }
        )


def get_execution_capabilities() -> ExecutionCapabilityReport:
    """Inspect capabilities without importing Parsl or PSI/J."""
    parsl = importlib.util.find_spec("parsl") is not None
    psij = importlib.util.find_spec("psij") is not None

    def optional(enabled: bool, package: str) -> CapabilityStatus:
        return CapabilityStatus(
            enabled,
            None if enabled else f"Install the {package!r} optional dependency.",
        )

    return ExecutionCapabilityReport(
        capabilities={
            "direct": CapabilityStatus(True),
            "wetlands": CapabilityStatus(True),
            "attached_parsl": optional(parsl, "parsl"),
            "submitted_local_parsl": optional(parsl, "parsl"),
            "submitted_remote_parsl": CapabilityStatus(True),
            "psij_launch": optional(psij, "psij"),
            "remote_profile_validation": CapabilityStatus(True),
            "portable_resource_overrides": CapabilityStatus(True),
            "non_allocating_planning": CapabilityStatus(True),
            "structured_node_failures": CapabilityStatus(True),
            "immutable_upload_preparation": CapabilityStatus(True),
        }
    )


__all__ = [
    "CapabilityStatus",
    "ExecutionCapabilityReport",
    "IntegrationDiagnostic",
    "NodeFailureDiagnostic",
    "ParslConfigValidationReport",
    "get_execution_capabilities",
    "validate_parsl_config_ref",
]
