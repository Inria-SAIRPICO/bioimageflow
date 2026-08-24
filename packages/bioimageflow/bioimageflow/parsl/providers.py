"""Versioned managed-worker adapters for supported Parsl providers."""

from __future__ import annotations

import importlib.metadata
import math
from dataclasses import dataclass
from typing import Any, ClassVar


_SUPPORTED_PARSL_RELEASE = (2026, 5)
_SCHEDULERS = frozenset({"slurm", "pbs", "lsf"})


class ManagedProviderError(ValueError):
    """A managed executor/provider tuple is unsupported or unsafe."""

    def __init__(self, category: str, message: str) -> None:
        self.category = category
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class ManagedProviderEvidence:
    """Sanitized, JSON-safe evidence for one managed executor route."""

    SCHEMA: ClassVar[str] = "bioimageflow.managed_provider_evidence.v1"

    label: str
    adapter: str
    adapter_version: int
    compatible_parsl: str
    parsl_version: str
    executor_type: str
    provider_type: str
    worker_scheduler: str
    orchestrator_scheduler: str
    worker_init_verified: bool
    provider_adapter_implemented: bool = True
    provider_adapter_compatible: bool = True
    shared_root: str = "runtime-unverified"
    nested_submission: str = "runtime-unverified"
    settings: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "label": self.label,
            "adapter": self.adapter,
            "adapter_version": self.adapter_version,
            "compatible_parsl": self.compatible_parsl,
            "parsl_version": self.parsl_version,
            "executor_type": self.executor_type,
            "provider_type": self.provider_type,
            "worker_scheduler": self.worker_scheduler,
            "orchestrator_scheduler": self.orchestrator_scheduler,
            "worker_init_verified": self.worker_init_verified,
            "provider_adapter_implemented": self.provider_adapter_implemented,
            "provider_adapter_compatible": self.provider_adapter_compatible,
            "shared_root": self.shared_root,
            "nested_submission": self.nested_submission,
            "settings": dict(self.settings or {}),
        }


@dataclass(frozen=True, slots=True)
class _ProviderAdapter:
    name: str
    executor_type: type[Any]
    provider_type: type[Any]
    worker_scheduler: str
    appends_worker_init_newline: bool = False

    def expected_worker_init(self, worker_init: str) -> str:
        if self.appends_worker_init_newline:
            return worker_init + "\n"
        return worker_init


def _type_name(value: Any) -> str:
    cls = type(value)
    return f"{cls.__module__}.{cls.__qualname__}"


def _parsl_version() -> str:
    try:
        return importlib.metadata.version("parsl")
    except importlib.metadata.PackageNotFoundError as exc:
        raise ManagedProviderError(
            "unsupported-managed-provider",
            "Managed provider validation requires the Parsl runtime.",
        ) from exc


def _release_tuple(version: str) -> tuple[int, int] | None:
    pieces = version.split(".", 2)
    if len(pieces) < 2 or not pieces[0].isdigit() or not pieces[1].isdigit():
        return None
    return int(pieces[0]), int(pieces[1])


def _adapters() -> tuple[_ProviderAdapter, ...]:
    # Imports stay local so importing bioimageflow.parsl does not require Parsl.
    from parsl.executors import HighThroughputExecutor
    from parsl.providers import (
        LSFProvider,
        PBSProProvider,
        SlurmProvider,
        TorqueProvider,
    )

    return (
        _ProviderAdapter(
            "parsl-htex-slurm.v1",
            HighThroughputExecutor,
            SlurmProvider,
            "slurm",
            appends_worker_init_newline=True,
        ),
        _ProviderAdapter(
            "parsl-htex-pbspro.v1",
            HighThroughputExecutor,
            PBSProProvider,
            "pbs",
        ),
        _ProviderAdapter(
            "parsl-htex-torque.v1",
            HighThroughputExecutor,
            TorqueProvider,
            "pbs",
        ),
        _ProviderAdapter(
            "parsl-htex-lsf.v1",
            HighThroughputExecutor,
            LSFProvider,
            "lsf",
        ),
    )


def _safe_number(value: Any) -> int | float | str | None:
    if value is None or type(value) is int:
        return value
    if type(value) is float:
        return value if math.isfinite(value) else "unbounded"
    return None


def _normalized_settings(executor: Any, provider: Any) -> dict[str, Any]:
    """Read only documented constructor-backed, non-shell public settings."""
    settings: dict[str, Any] = {
        "executor": {
            "cores_per_worker": _safe_number(executor.cores_per_worker),
            "mem_per_worker": _safe_number(executor.mem_per_worker),
            "max_workers_per_node": _safe_number(executor.max_workers_per_node),
            "encrypted": bool(executor.encrypted),
        },
        "provider": {
            "nodes_per_block": provider.nodes_per_block,
            "init_blocks": provider.init_blocks,
            "min_blocks": provider.min_blocks,
            "max_blocks": provider.max_blocks,
            "parallelism": _safe_number(provider.parallelism),
            "walltime": provider.walltime,
        },
    }
    provider_settings = settings["provider"]
    for name in (
        "partition",
        "account",
        "qos",
        "constraint",
        "clusters",
        "cores_per_node",
        "mem_per_node",
        "gpus_per_node",
        "exclusive",
        "queue",
        "cpus_per_node",
        "select_options",
        "project",
        "cores_per_block",
        "request_by_nodes",
        "bsub_redirection",
    ):
        if hasattr(provider, name):
            value = getattr(provider, name)
            if value is None or type(value) in {str, int, float, bool}:
                provider_settings[name] = _safe_number(value) if type(value) is float else value
    return settings


def validate_managed_provider(
    executor: Any,
    *,
    worker_init: str,
    orchestrator_scheduler: str,
) -> ManagedProviderEvidence:
    """Validate one exact supported public executor/provider combination."""
    if orchestrator_scheduler not in _SCHEDULERS:
        raise ManagedProviderError(
            "unsupported-managed-provider",
            f"Unknown orchestrator scheduler {orchestrator_scheduler!r}.",
        )
    version = _parsl_version()
    if _release_tuple(version) != _SUPPORTED_PARSL_RELEASE:
        raise ManagedProviderError(
            "unsupported-managed-provider",
            f"Parsl {version} has no managed provider adapter.",
        )
    provider = getattr(executor, "provider", None)
    adapter = next(
        (
            candidate
            for candidate in _adapters()
            if type(executor) is candidate.executor_type
            and type(provider) is candidate.provider_type
        ),
        None,
    )
    if adapter is None:
        raise ManagedProviderError(
            "unsupported-managed-provider",
            "The managed executor/provider combination is unsupported.",
        )
    assert provider is not None
    if adapter.worker_scheduler != orchestrator_scheduler:
        raise ManagedProviderError(
            "unsupported-managed-provider",
            f"The {adapter.worker_scheduler!r} worker provider is incompatible "
            f"with the {orchestrator_scheduler!r} orchestrator scheduler.",
        )
    actual_worker_init = provider.worker_init
    if actual_worker_init != adapter.expected_worker_init(worker_init):
        raise ManagedProviderError(
            "worker-initialization-missing",
            "The managed provider does not contain the exact generated worker initialization.",
        )
    label = getattr(executor, "label", None)
    if type(label) is not str or not label:
        raise ManagedProviderError(
            "executor-label-mismatch",
            "Managed executors require a non-empty public label.",
        )
    return ManagedProviderEvidence(
        label=label,
        adapter=adapter.name,
        adapter_version=1,
        compatible_parsl=">=2026.5.25,<2026.6",
        parsl_version=version,
        executor_type=_type_name(executor),
        provider_type=_type_name(provider),
        worker_scheduler=adapter.worker_scheduler,
        orchestrator_scheduler=orchestrator_scheduler,
        worker_init_verified=True,
        settings=_normalized_settings(executor, provider),
    )


__all__ = [
    "ManagedProviderError",
    "ManagedProviderEvidence",
    "validate_managed_provider",
]
