"""Strict executor-preflight expectations and result validation."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar

from bioimageflow_core.worker_origins import (
    ArchiveModuleOriginV1,
    SharedModuleOriginV1,
    SourceFileOriginV1,
    VersionedModuleOriginV1,
    WorkerToolOriginV1,
    encode_worker_tool_origin,
    worker_tool_origin_identity,
)
from bioimageflow_core.preflight import (
    PREFLIGHT_RESULT_SCHEMA,
    PREFLIGHT_SCHEMA,
)
from bioimageflow_core.worker_protocol import TASK_SCHEMA

from .requirements import normalize_core_requirement
from .routing import RoutingPlan


WORKER_API = TASK_SCHEMA


class ParslPreflightError(RuntimeError):
    """An executor preflight result is malformed or disproves compatibility."""


def _exact_dict(
    value: Any,
    expected: frozenset[str],
    *,
    label: str,
) -> dict[str, Any]:
    if type(value) is not dict:
        raise ParslPreflightError(f"{label} must be a plain object.")
    if not all(type(key) is str for key in value):
        raise ParslPreflightError(f"{label} keys must be strings.")
    actual = frozenset(value)
    if actual != expected:
        raise ParslPreflightError(
            f"Malformed {label}; missing={sorted(expected - actual)}, "
            f"extra={sorted(actual - expected)}."
        )
    return value


def _text(value: Any, *, field: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ParslPreflightError(
            f"{field} must be a non-empty, trimmed string."
        )
    return value


def _bool(value: Any, *, field: str) -> bool:
    if type(value) is not bool:
        raise ParslPreflightError(f"{field} must be a boolean.")
    return value


def _absolute_path(value: Any, *, field: str) -> str:
    text = _text(value, field=field)
    path = Path(text)
    if not path.is_absolute():
        raise ParslPreflightError(f"{field} must be an absolute path.")
    normalized = str(path.resolve(strict=False))
    if normalized != text:
        raise ParslPreflightError(f"{field} must be normalized.")
    return text


def _string_array(value: Any, *, field: str) -> tuple[str, ...]:
    if type(value) is not list:
        raise ParslPreflightError(f"{field} must be a JSON array.")
    items = tuple(_text(item, field=field) for item in value)
    if len(items) != len(set(items)):
        raise ParslPreflightError(f"{field} must not contain duplicates.")
    return items


@dataclass(frozen=True, slots=True)
class PreflightPathResultV1:
    """One worker-observed shared path."""

    path: str
    resolved_path: str
    readable: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "resolved_path": self.resolved_path,
            "readable": self.readable,
        }

    @classmethod
    def from_dict(cls, value: Any) -> "PreflightPathResultV1":
        data = _exact_dict(
            value,
            frozenset({"path", "resolved_path", "readable"}),
            label="preflight path result",
        )
        return cls(
            path=_absolute_path(data["path"], field="path"),
            resolved_path=_absolute_path(
                data["resolved_path"],
                field="resolved_path",
            ),
            readable=_bool(data["readable"], field="readable"),
        )


@dataclass(frozen=True, slots=True)
class PreflightOriginResultV1:
    """One worker-resolved complete origin identity."""

    identity: str
    kind: str
    resolved: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "identity": self.identity,
            "kind": self.kind,
            "resolved": self.resolved,
        }

    @classmethod
    def from_dict(cls, value: Any) -> "PreflightOriginResultV1":
        data = _exact_dict(
            value,
            frozenset({"identity", "kind", "resolved"}),
            label="preflight origin result",
        )
        identity = _text(data["identity"], field="origin identity")
        if (
            len(identity) != 64
            or any(character not in "0123456789abcdef" for character in identity)
        ):
            raise ParslPreflightError(
                "origin identity must be a lowercase SHA-256 digest."
            )
        return cls(
            identity=identity,
            kind=_text(data["kind"], field="origin kind"),
            resolved=_bool(data["resolved"], field="origin resolved"),
        )


@dataclass(frozen=True, slots=True)
class ExecutorPreflightResultV1:
    """Strict successful-result envelope returned by one executor probe."""

    SCHEMA: ClassVar[str] = PREFLIGHT_RESULT_SCHEMA

    executor_label: str
    worker_api: str
    core_version: str
    core_requirements: tuple[str, ...]
    core_compatible: bool
    storage_root: str
    sentinel_path: str
    sentinel_write: bool
    sentinel_read: bool
    sentinel_delete: bool
    path_results: tuple[PreflightPathResultV1, ...]
    origin_results: tuple[PreflightOriginResultV1, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "executor_label": self.executor_label,
            "worker_api": self.worker_api,
            "core_version": self.core_version,
            "core_requirements": list(self.core_requirements),
            "core_compatible": self.core_compatible,
            "storage_root": self.storage_root,
            "sentinel_path": self.sentinel_path,
            "sentinel_write": self.sentinel_write,
            "sentinel_read": self.sentinel_read,
            "sentinel_delete": self.sentinel_delete,
            "path_results": [
                result.to_dict() for result in self.path_results
            ],
            "origin_results": [
                result.to_dict() for result in self.origin_results
            ],
        }

    @classmethod
    def from_dict(cls, value: Any) -> "ExecutorPreflightResultV1":
        data = _exact_dict(
            value,
            frozenset(
                {
                    "schema",
                    "executor_label",
                    "worker_api",
                    "core_version",
                    "core_requirements",
                    "core_compatible",
                    "storage_root",
                    "sentinel_path",
                    "sentinel_write",
                    "sentinel_read",
                    "sentinel_delete",
                    "path_results",
                    "origin_results",
                }
            ),
            label="executor preflight result",
        )
        if data["schema"] != cls.SCHEMA:
            raise ParslPreflightError(
                f"Unknown executor preflight schema {data['schema']!r}."
            )
        path_results = data["path_results"]
        origin_results = data["origin_results"]
        if type(path_results) is not list:
            raise ParslPreflightError("path_results must be a JSON array.")
        if type(origin_results) is not list:
            raise ParslPreflightError("origin_results must be a JSON array.")
        decoded_paths = tuple(
            PreflightPathResultV1.from_dict(item) for item in path_results
        )
        decoded_origins = tuple(
            PreflightOriginResultV1.from_dict(item) for item in origin_results
        )
        path_names = [item.path for item in decoded_paths]
        origin_identities = [item.identity for item in decoded_origins]
        if len(path_names) != len(set(path_names)):
            raise ParslPreflightError(
                "path_results must not contain duplicate paths."
            )
        if len(origin_identities) != len(set(origin_identities)):
            raise ParslPreflightError(
                "origin_results must not contain duplicate identities."
            )
        return cls(
            executor_label=_text(
                data["executor_label"],
                field="executor_label",
            ),
            worker_api=_text(data["worker_api"], field="worker_api"),
            core_version=_text(data["core_version"], field="core_version"),
            core_requirements=_string_array(
                data["core_requirements"],
                field="core_requirements",
            ),
            core_compatible=_bool(
                data["core_compatible"],
                field="core_compatible",
            ),
            storage_root=_absolute_path(
                data["storage_root"],
                field="storage_root",
            ),
            sentinel_path=_absolute_path(
                data["sentinel_path"],
                field="sentinel_path",
            ),
            sentinel_write=_bool(
                data["sentinel_write"],
                field="sentinel_write",
            ),
            sentinel_read=_bool(
                data["sentinel_read"],
                field="sentinel_read",
            ),
            sentinel_delete=_bool(
                data["sentinel_delete"],
                field="sentinel_delete",
            ),
            path_results=decoded_paths,
            origin_results=decoded_origins,
        )


def _origin_paths(origin: WorkerToolOriginV1) -> tuple[str, ...]:
    if isinstance(origin, VersionedModuleOriginV1):
        return (origin.store_root,)
    if isinstance(origin, SharedModuleOriginV1):
        return (origin.import_root,)
    if isinstance(origin, SourceFileOriginV1):
        return (origin.path,)
    if isinstance(origin, ArchiveModuleOriginV1):
        return (origin.materialization_root,)
    return ()


@dataclass(frozen=True, slots=True)
class PreflightExpectation:
    """Orchestrator-owned facts one executor probe must echo and prove."""

    executor_label: str
    environment_identities: tuple[str, ...]
    core_requirements: tuple[str, ...]
    storage_root: str
    sentinel_path: str
    readable_paths: tuple[str, ...]
    origins: tuple[WorkerToolOriginV1, ...]
    expected_core_version: str | None = None

    @property
    def origin_identities(self) -> dict[str, str]:
        return {
            worker_tool_origin_identity(origin): origin.kind
            for origin in self.origins
        }


def _normalized_expected_path(value: str | Path, *, field: str) -> str:
    if not isinstance(value, (str, Path)):
        raise TypeError(f"{field} must be a string or Path.")
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ParslPreflightError(f"{field} must be an absolute path.")
    return str(path.resolve(strict=False))


def build_preflight_expectation(
    plan: RoutingPlan,
    executor_label: str,
    *,
    storage_root: str | Path,
    sentinel_path: str | Path,
    additional_readable_paths: Iterable[str | Path] = (),
    expected_core_version: str | None = None,
) -> PreflightExpectation:
    """Build one exact probe expectation from all routes on an executor."""
    if type(plan) is not RoutingPlan:
        raise TypeError("plan must be a RoutingPlan.")
    if type(executor_label) is not str or not executor_label:
        raise TypeError("executor_label must be a non-empty string.")
    routes = [
        route for route in plan.routes if route.executor_label == executor_label
    ]
    if not routes:
        raise ParslPreflightError(
            f"Routing plan does not select executor {executor_label!r}."
        )
    normalized_storage = _normalized_expected_path(
        storage_root,
        field="storage_root",
    )
    normalized_sentinel = _normalized_expected_path(
        sentinel_path,
        field="sentinel_path",
    )
    try:
        relative_sentinel = Path(normalized_sentinel).relative_to(
            normalized_storage
        )
    except ValueError as exc:
        raise ParslPreflightError(
            "sentinel_path must be confined beneath storage_root."
        ) from exc
    if (
        not relative_sentinel.parts
        or relative_sentinel.parts[0] in {"cache", "views", "outputs"}
    ):
        raise ParslPreflightError(
            "sentinel_path must use a non-cache namespace beneath storage_root."
        )
    if expected_core_version is not None and (
        type(expected_core_version) is not str
        or not expected_core_version
        or expected_core_version != expected_core_version.strip()
    ):
        raise ParslPreflightError(
            "expected_core_version must be a non-empty, trimmed string or None."
        )

    requirements = [route.requirement for route in routes]
    origins_by_identity = {
        worker_tool_origin_identity(requirement.tool_origin): requirement.tool_origin
        for requirement in requirements
    }
    readable_paths = {normalized_storage}
    for requirement in requirements:
        readable_paths.update(requirement.anchored_dependency_paths)
        readable_paths.update(_origin_paths(requirement.tool_origin))
    readable_paths.update(
        _normalized_expected_path(path, field="additional_readable_path")
        for path in additional_readable_paths
    )
    core_requirements = tuple(
        sorted(
            {
                normalize_core_requirement(requirement.core_requirement)
                for requirement in requirements
            }
        )
    )
    return PreflightExpectation(
        executor_label=executor_label,
        environment_identities=tuple(
            sorted({requirement.environment_identity for requirement in requirements})
        ),
        core_requirements=core_requirements,
        storage_root=normalized_storage,
        sentinel_path=normalized_sentinel,
        readable_paths=tuple(sorted(readable_paths)),
        origins=tuple(
            origins_by_identity[identity] for identity in sorted(origins_by_identity)
        ),
        expected_core_version=expected_core_version,
    )


def build_preflight_payload(
    expectation: PreflightExpectation,
) -> dict[str, Any]:
    """Encode one exact worker-safe executor preflight request."""
    if type(expectation) is not PreflightExpectation:
        raise TypeError("expectation must be a PreflightExpectation.")
    return {
        "schema": PREFLIGHT_SCHEMA,
        "executor_label": expectation.executor_label,
        "core_requirements": list(expectation.core_requirements),
        "storage_root": expectation.storage_root,
        "sentinel_path": expectation.sentinel_path,
        "readable_paths": list(expectation.readable_paths),
        "origins": [
            encode_worker_tool_origin(origin) for origin in expectation.origins
        ],
    }


def _failure_prefix(expectation: PreflightExpectation) -> str:
    return (
        f"Executor preflight {expectation.executor_label!r} for environments "
        f"{list(expectation.environment_identities)}"
    )


def validate_preflight_result(
    value: Any,
    expectation: PreflightExpectation,
) -> ExecutorPreflightResultV1:
    """Validate exact executor, core, path, sentinel, and origin evidence."""
    if type(expectation) is not PreflightExpectation:
        raise TypeError("expectation must be a PreflightExpectation.")
    result = ExecutorPreflightResultV1.from_dict(
        value.to_dict()
        if type(value) is ExecutorPreflightResultV1
        else value
    )
    prefix = _failure_prefix(expectation)
    if result.executor_label != expectation.executor_label:
        raise ParslPreflightError(
            f"{prefix} ran on executor {result.executor_label!r}."
        )
    if result.worker_api != WORKER_API:
        raise ParslPreflightError(
            f"{prefix} reported incompatible worker API "
            f"{result.worker_api!r}; expected {WORKER_API!r}."
        )
    reported_requirements = tuple(
        sorted(normalize_core_requirement(item) for item in result.core_requirements)
    )
    if reported_requirements != expectation.core_requirements:
        raise ParslPreflightError(
            f"{prefix} checked core requirements {reported_requirements}, not "
            f"{expectation.core_requirements}."
        )
    if not result.core_compatible:
        raise ParslPreflightError(
            f"{prefix} reported incompatible bioimageflow-core "
            f"{result.core_version!r}."
        )
    if (
        expectation.expected_core_version is not None
        and result.core_version != expectation.expected_core_version
    ):
        raise ParslPreflightError(
            f"{prefix} reported core version {result.core_version!r}, not "
            f"{expectation.expected_core_version!r}."
        )
    if result.storage_root != expectation.storage_root:
        raise ParslPreflightError(
            f"{prefix} observed storage root {result.storage_root!r}, not "
            f"{expectation.storage_root!r}."
        )
    if result.sentinel_path != expectation.sentinel_path:
        raise ParslPreflightError(
            f"{prefix} observed sentinel {result.sentinel_path!r}, not "
            f"{expectation.sentinel_path!r}."
        )
    failed_sentinel_actions = [
        name
        for name, succeeded in (
            ("write", result.sentinel_write),
            ("read", result.sentinel_read),
            ("delete", result.sentinel_delete),
        )
        if not succeeded
    ]
    if failed_sentinel_actions:
        raise ParslPreflightError(
            f"{prefix} failed sentinel capabilities "
            f"{failed_sentinel_actions} at {expectation.sentinel_path!r}."
        )

    paths = {path.path: path for path in result.path_results}
    if tuple(sorted(paths)) != expectation.readable_paths:
        raise ParslPreflightError(
            f"{prefix} returned shared paths {sorted(paths)}, not "
            f"{list(expectation.readable_paths)}."
        )
    for path, evidence in sorted(paths.items()):
        if evidence.resolved_path != path:
            raise ParslPreflightError(
                f"{prefix} resolves shared path {path!r} as "
                f"{evidence.resolved_path!r}."
            )
        if not evidence.readable:
            raise ParslPreflightError(
                f"{prefix} cannot read shared path {path!r}."
            )

    expected_origins = expectation.origin_identities
    origins = {origin.identity: origin for origin in result.origin_results}
    if set(origins) != set(expected_origins):
        raise ParslPreflightError(
            f"{prefix} returned origin identities {sorted(origins)}, not "
            f"{sorted(expected_origins)}."
        )
    for identity, expected_kind in sorted(expected_origins.items()):
        evidence = origins[identity]
        if evidence.kind != expected_kind or not evidence.resolved:
            raise ParslPreflightError(
                f"{prefix} failed {expected_kind!r} origin {identity!r}; "
                f"reported kind={evidence.kind!r}, resolved={evidence.resolved}."
            )
    return result


def validate_preflight_results(
    values: Mapping[str, Any],
    expectations: Mapping[str, PreflightExpectation],
) -> dict[str, ExecutorPreflightResultV1]:
    """Validate exactly one result for every selected executor label."""
    if not isinstance(values, Mapping) or not isinstance(expectations, Mapping):
        raise TypeError("values and expectations must be mappings.")
    actual_labels = set(values)
    expected_labels = set(expectations)
    if actual_labels != expected_labels:
        raise ParslPreflightError(
            f"Preflight result labels do not match selected executors; "
            f"missing={sorted(expected_labels - actual_labels)}, "
            f"extra={sorted(actual_labels - expected_labels)}."
        )
    return {
        label: validate_preflight_result(values[label], expectations[label])
        for label in sorted(expected_labels)
    }


__all__ = [
    "ExecutorPreflightResultV1",
    "ParslPreflightError",
    "PreflightExpectation",
    "PreflightOriginResultV1",
    "PreflightPathResultV1",
    "WORKER_API",
    "build_preflight_expectation",
    "build_preflight_payload",
    "validate_preflight_result",
    "validate_preflight_results",
]
