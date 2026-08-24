"""Managed-cluster Parsl factory values and runtime helper."""

from __future__ import annotations

import hashlib
import shlex
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import PurePosixPath
from types import MappingProxyType
from typing import Any, ClassVar

from bioimageflow.storage import canonical_json_bytes

from .requirements import parse_memory_bytes
from .types import (
    ExecutorBinding,
    ExecutorCapabilities,
    WorkerEnvironmentAttestation,
    WorkerSlotCapacity,
)


def _capacity(value: str | int | None, *, field: str) -> int | None:
    if value is None:
        return None
    if type(value) is int:
        if value <= 0:
            raise ValueError(f"{field} must be positive.")
        return value
    if type(value) is str:
        return parse_memory_bytes("".join(value.split()), field=field)
    raise TypeError(f"{field} must be a byte string, integer, or None.")


@dataclass(frozen=True, slots=True)
class WorkerSlot:
    """Resources guaranteed to one concurrent BioImageFlow task."""

    SCHEMA: ClassVar[str] = "bioimageflow.worker_slot.v1"

    cpu: int = 1
    gpu: int = 0
    memory: str | int | None = None
    gpu_memory: str | int | None = None

    def __post_init__(self) -> None:
        if type(self.cpu) is not int or self.cpu <= 0:
            raise ValueError("cpu must be a positive integer.")
        if type(self.gpu) is not int or self.gpu < 0:
            raise ValueError("gpu must be a non-negative integer.")
        object.__setattr__(self, "memory", _capacity(self.memory, field="memory"))
        object.__setattr__(
            self,
            "gpu_memory",
            _capacity(self.gpu_memory, field="gpu_memory"),
        )

    @property
    def memory_bytes(self) -> int | None:
        return self.memory if type(self.memory) is int else None

    @property
    def gpu_memory_bytes(self) -> int | None:
        return self.gpu_memory if type(self.gpu_memory) is int else None

    def to_capacity(self) -> WorkerSlotCapacity:
        return WorkerSlotCapacity(
            cpu=self.cpu,
            gpu=self.gpu,
            memory_bytes=self.memory_bytes,
            gpu_memory_bytes=self.gpu_memory_bytes,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "cpu": self.cpu,
            "gpu": self.gpu,
            "memory_bytes": self.memory_bytes,
            "gpu_memory_bytes": self.gpu_memory_bytes,
        }

    @classmethod
    def from_dict(cls, value: Any) -> "WorkerSlot":
        if type(value) is not dict or set(value) != {
            "schema",
            "cpu",
            "gpu",
            "memory_bytes",
            "gpu_memory_bytes",
        } or value["schema"] != cls.SCHEMA:
            raise ValueError("Invalid WorkerSlot payload.")
        return cls(
            cpu=value["cpu"],
            gpu=value["gpu"],
            memory=value["memory_bytes"],
            gpu_memory=value["gpu_memory_bytes"],
        )


@dataclass(frozen=True, slots=True)
class ParslFactoryResult:
    """The inseparable live Parsl Config and BioImageFlow executor bindings."""

    config: Any
    executor_bindings: Mapping[str, ExecutorBinding]

    def __post_init__(self) -> None:
        if self.config is None:
            raise TypeError("config must be a live parsl.Config value.")
        if not isinstance(self.executor_bindings, Mapping) or not self.executor_bindings:
            raise ValueError("executor_bindings must be a non-empty mapping.")
        bindings = dict(self.executor_bindings)
        if any(
            type(label) is not str
            or not label
            or type(binding) is not ExecutorBinding
            or binding.label not in {label, "managed"}
            for label, binding in bindings.items()
        ):
            raise ValueError(
                "Executor binding keys and binding labels must match, except "
                "for managed runtime placeholders."
            )
        object.__setattr__(self, "executor_bindings", MappingProxyType(bindings))


@dataclass(frozen=True, slots=True, repr=False)
class ParslFactoryRuntime:
    """Read-only deployment facts supplied to a trusted Parsl factory."""

    deployment_root: PurePosixPath
    deployment_id: str
    worker_init: str
    environment_name: str
    environment_identity: str
    core_requirement: str
    storage_mode: str = "shared_fs"
    tool_origin_modes: tuple[str, ...] = (
        "installed_module",
        "versioned_module",
        "shared_module",
        "source_file",
        "archive_module",
    )

    def __post_init__(self) -> None:
        if not isinstance(self.deployment_root, PurePosixPath) or not self.deployment_root.is_absolute():
            raise ValueError("deployment_root must be an absolute POSIX path.")
        if self.storage_mode != "shared_fs":
            raise ValueError("Managed factory runtime currently requires shared_fs.")
        for field_name in (
            "deployment_id",
            "worker_init",
            "environment_name",
            "environment_identity",
            "core_requirement",
        ):
            value = getattr(self, field_name)
            if type(value) is not str or not value:
                raise ValueError(f"{field_name} must be a non-empty string.")

    def __repr__(self) -> str:
        return (
            "ParslFactoryRuntime("
            f"deployment_id={self.deployment_id!r}, "
            f"environment_name={self.environment_name!r}, "
            f"storage_mode={self.storage_mode!r})"
        )

    def executor_binding(self, *, slot: WorkerSlot) -> ExecutorBinding:
        if type(slot) is not WorkerSlot:
            raise TypeError("slot must be WorkerSlot.")
        digest = self.environment_identity
        if digest.startswith("sha256:"):
            digest = digest.removeprefix("sha256:")
        if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
            digest = hashlib.sha256(canonical_json_bytes(self.environment_identity)).hexdigest()
        # The result is relabeled by the factory author through the mapping key.
        # A placeholder label is replaced and validated by normalize_factory_result().
        return ExecutorBinding(
            label="managed",
            environments=(
                WorkerEnvironmentAttestation(
                    name=self.environment_name,
                    dependency_hash=digest,
                    allow_flexible_versions=False,
                    core_requirement=self.core_requirement,
                ),
            ),
            capabilities=ExecutorCapabilities(
                storage_modes=(self.storage_mode,),
                tool_origin_modes=self.tool_origin_modes,
                slot=slot.to_capacity(),
            ),
        )


def normalize_factory_result(result: ParslFactoryResult) -> ParslFactoryResult:
    """Bind managed placeholder labels to their mapping keys and reject drift."""
    if type(result) is not ParslFactoryResult:
        raise TypeError("Parsl factory must return exactly ParslFactoryResult.")
    normalized: dict[str, ExecutorBinding] = {}
    for label, binding in result.executor_bindings.items():
        if binding.label == label:
            normalized[label] = binding
            continue
        if binding.label != "managed":
            raise ValueError("Executor binding labels must match their mapping keys.")
        normalized[label] = ExecutorBinding(
            label=label,
            environments=binding.environments,
            capabilities=binding.capabilities,
        )
    return ParslFactoryResult(config=result.config, executor_bindings=normalized)


def managed_worker_init(
    *,
    setup_path: PurePosixPath | None,
    activation_path: PurePosixPath,
    deployment_id: str,
) -> str:
    """Generate the exact shell fragment required by supported providers."""
    commands = ["set -eu"]
    if setup_path is not None:
        commands.append(f". {shlex.quote(str(setup_path))}")
    commands.append(f". {shlex.quote(str(activation_path))}")
    commands.append(
        "export BIOIMAGEFLOW_DEPLOYMENT_ID=" + shlex.quote(deployment_id)
    )
    return "\n".join(commands) + "\n"


__all__ = [
    "ParslFactoryResult",
    "ParslFactoryRuntime",
    "WorkerSlot",
    "managed_worker_init",
    "normalize_factory_result",
]
