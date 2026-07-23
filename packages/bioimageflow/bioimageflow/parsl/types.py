"""Strict JSON-safe configuration values for the Parsl backend."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, ClassVar


_STORAGE_MODES = frozenset({"shared_fs", "staged"})
_TOOL_ORIGIN_MODES = frozenset(
    {
        "installed_module",
        "versioned_module",
        "shared_module",
        "source_file",
        "archive_module",
    }
)


def _require_exact_keys(
    value: Any,
    expected: frozenset[str],
    *,
    type_name: str,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise TypeError(f"{type_name} must be decoded from a dictionary.")
    if not all(type(key) is str for key in value):
        raise TypeError(f"{type_name} keys must be strings.")
    actual = frozenset(value)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise ValueError(
            f"Invalid {type_name} keys; missing={missing}, extra={extra}."
        )
    return value


def _require_schema(value: Any, expected: str, *, type_name: str) -> None:
    if type(value) is not str or value != expected:
        raise ValueError(
            f"Unknown {type_name} schema {value!r}; expected {expected!r}."
        )


def _require_int(
    value: Any,
    *,
    field: str,
    minimum: int,
) -> int:
    if type(value) is not int:
        raise TypeError(f"{field} must be an integer.")
    if value < minimum:
        raise ValueError(f"{field} must be greater than or equal to {minimum}.")
    return value


def _require_optional_capacity(value: Any, *, field: str) -> int | None:
    if value is None:
        return None
    return _require_int(value, field=field, minimum=1)


def _require_nonempty_string(value: Any, *, field: str) -> str:
    if type(value) is not str:
        raise TypeError(f"{field} must be a string.")
    if not value or value != value.strip():
        raise ValueError(f"{field} must be a non-empty, trimmed string.")
    return value


def _require_string_tuple(
    value: Any,
    *,
    field: str,
    allowed: frozenset[str],
) -> tuple[str, ...]:
    if type(value) is not tuple:
        raise TypeError(f"{field} must be a tuple.")
    if not value:
        raise ValueError(f"{field} must not be empty.")
    for item in value:
        if type(item) is not str or item not in allowed:
            raise ValueError(
                f"Unknown {field} value {item!r}; expected one of "
                f"{sorted(allowed)}."
            )
    if len(value) != len(set(value)):
        raise ValueError(f"{field} must not contain duplicates.")
    return value


@dataclass(frozen=True, slots=True)
class ParslTaskPolicy:
    """Bound task packing and the number of unfinished Parsl futures."""

    SCHEMA: ClassVar[str] = "bioimageflow.parsl.task_policy.v1"

    row_chunk_size: int = 1
    max_in_flight: int = 32

    def __post_init__(self) -> None:
        _require_int(self.row_chunk_size, field="row_chunk_size", minimum=1)
        _require_int(self.max_in_flight, field="max_in_flight", minimum=1)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "row_chunk_size": self.row_chunk_size,
            "max_in_flight": self.max_in_flight,
        }

    @classmethod
    def from_dict(cls, value: Any) -> "ParslTaskPolicy":
        data = _require_exact_keys(
            value,
            frozenset({"schema", "row_chunk_size", "max_in_flight"}),
            type_name=cls.__name__,
        )
        _require_schema(data["schema"], cls.SCHEMA, type_name=cls.__name__)
        return cls(
            row_chunk_size=data["row_chunk_size"],
            max_in_flight=data["max_in_flight"],
        )


@dataclass(frozen=True, slots=True)
class WorkerSlotCapacity:
    """Resources guaranteed to one worker slot on an executor."""

    SCHEMA: ClassVar[str] = "bioimageflow.parsl.worker_slot_capacity.v1"

    cpu: int
    gpu: int = 0
    memory_bytes: int | None = None
    gpu_memory_bytes: int | None = None

    def __post_init__(self) -> None:
        _require_int(self.cpu, field="cpu", minimum=1)
        _require_int(self.gpu, field="gpu", minimum=0)
        _require_optional_capacity(self.memory_bytes, field="memory_bytes")
        _require_optional_capacity(
            self.gpu_memory_bytes,
            field="gpu_memory_bytes",
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
    def from_dict(cls, value: Any) -> "WorkerSlotCapacity":
        data = _require_exact_keys(
            value,
            frozenset(
                {
                    "schema",
                    "cpu",
                    "gpu",
                    "memory_bytes",
                    "gpu_memory_bytes",
                }
            ),
            type_name=cls.__name__,
        )
        _require_schema(data["schema"], cls.SCHEMA, type_name=cls.__name__)
        return cls(
            cpu=data["cpu"],
            gpu=data["gpu"],
            memory_bytes=data["memory_bytes"],
            gpu_memory_bytes=data["gpu_memory_bytes"],
        )


@dataclass(frozen=True, slots=True)
class ExecutorCapabilities:
    """Storage, tool-origin, and slot capabilities attested by an executor."""

    SCHEMA: ClassVar[str] = "bioimageflow.parsl.executor_capabilities.v1"

    storage_modes: tuple[str, ...]
    tool_origin_modes: tuple[str, ...]
    slot: WorkerSlotCapacity

    def __post_init__(self) -> None:
        _require_string_tuple(
            self.storage_modes,
            field="storage_modes",
            allowed=_STORAGE_MODES,
        )
        _require_string_tuple(
            self.tool_origin_modes,
            field="tool_origin_modes",
            allowed=_TOOL_ORIGIN_MODES,
        )
        if type(self.slot) is not WorkerSlotCapacity:
            raise TypeError("slot must be a WorkerSlotCapacity.")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "storage_modes": list(self.storage_modes),
            "tool_origin_modes": list(self.tool_origin_modes),
            "slot": self.slot.to_dict(),
        }

    @classmethod
    def from_dict(cls, value: Any) -> "ExecutorCapabilities":
        data = _require_exact_keys(
            value,
            frozenset(
                {
                    "schema",
                    "storage_modes",
                    "tool_origin_modes",
                    "slot",
                }
            ),
            type_name=cls.__name__,
        )
        _require_schema(data["schema"], cls.SCHEMA, type_name=cls.__name__)
        storage_modes = data["storage_modes"]
        tool_origin_modes = data["tool_origin_modes"]
        if type(storage_modes) is not list:
            raise TypeError("storage_modes must be a JSON array.")
        if type(tool_origin_modes) is not list:
            raise TypeError("tool_origin_modes must be a JSON array.")
        return cls(
            storage_modes=tuple(storage_modes),
            tool_origin_modes=tuple(tool_origin_modes),
            slot=WorkerSlotCapacity.from_dict(data["slot"]),
        )


@dataclass(frozen=True, slots=True)
class WorkerEnvironmentAttestation:
    """Deployment claim for one worker environment identity."""

    SCHEMA: ClassVar[str] = (
        "bioimageflow.parsl.worker_environment_attestation.v1"
    )

    name: str
    dependency_hash: str
    allow_flexible_versions: bool
    core_requirement: str

    def __post_init__(self) -> None:
        _require_nonempty_string(self.name, field="name")
        dependency_hash = _require_nonempty_string(
            self.dependency_hash,
            field="dependency_hash",
        )
        if (
            len(dependency_hash) != 64
            or any(character not in "0123456789abcdef" for character in dependency_hash)
        ):
            raise ValueError(
                "dependency_hash must contain exactly 64 lowercase hexadecimal "
                "characters."
            )
        if type(self.allow_flexible_versions) is not bool:
            raise TypeError("allow_flexible_versions must be a boolean.")
        _require_nonempty_string(
            self.core_requirement,
            field="core_requirement",
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "name": self.name,
            "dependency_hash": self.dependency_hash,
            "allow_flexible_versions": self.allow_flexible_versions,
            "core_requirement": self.core_requirement,
        }

    @classmethod
    def from_dict(cls, value: Any) -> "WorkerEnvironmentAttestation":
        data = _require_exact_keys(
            value,
            frozenset(
                {
                    "schema",
                    "name",
                    "dependency_hash",
                    "allow_flexible_versions",
                    "core_requirement",
                }
            ),
            type_name=cls.__name__,
        )
        _require_schema(data["schema"], cls.SCHEMA, type_name=cls.__name__)
        return cls(
            name=data["name"],
            dependency_hash=data["dependency_hash"],
            allow_flexible_versions=data["allow_flexible_versions"],
            core_requirement=data["core_requirement"],
        )


@dataclass(frozen=True, slots=True)
class ExecutorBinding:
    """Immutable executor label, environment, and capability declaration."""

    SCHEMA: ClassVar[str] = "bioimageflow.parsl.executor_binding.v1"

    label: str
    environments: tuple[WorkerEnvironmentAttestation, ...]
    capabilities: ExecutorCapabilities

    def __post_init__(self) -> None:
        _require_nonempty_string(self.label, field="label")
        if type(self.environments) is not tuple:
            raise TypeError("environments must be a tuple.")
        if not self.environments:
            raise ValueError("environments must not be empty.")
        if not all(
            type(environment) is WorkerEnvironmentAttestation
            for environment in self.environments
        ):
            raise TypeError(
                "environments must contain WorkerEnvironmentAttestation values."
            )
        identities = {
            (
                environment.name,
                environment.dependency_hash,
                environment.allow_flexible_versions,
            )
            for environment in self.environments
        }
        if len(identities) != len(self.environments):
            raise ValueError("environments must not contain duplicate identities.")
        if type(self.capabilities) is not ExecutorCapabilities:
            raise TypeError("capabilities must be ExecutorCapabilities.")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "label": self.label,
            "environments": [
                environment.to_dict() for environment in self.environments
            ],
            "capabilities": self.capabilities.to_dict(),
        }

    @classmethod
    def from_dict(cls, value: Any) -> "ExecutorBinding":
        data = _require_exact_keys(
            value,
            frozenset(
                {
                    "schema",
                    "label",
                    "environments",
                    "capabilities",
                }
            ),
            type_name=cls.__name__,
        )
        _require_schema(data["schema"], cls.SCHEMA, type_name=cls.__name__)
        environments = data["environments"]
        if type(environments) is not list:
            raise TypeError("environments must be a JSON array.")
        return cls(
            label=data["label"],
            environments=tuple(
                WorkerEnvironmentAttestation.from_dict(environment)
                for environment in environments
            ),
            capabilities=ExecutorCapabilities.from_dict(data["capabilities"]),
        )


__all__ = [
    "ExecutorBinding",
    "ExecutorCapabilities",
    "ParslTaskPolicy",
    "WorkerEnvironmentAttestation",
    "WorkerSlotCapacity",
]
