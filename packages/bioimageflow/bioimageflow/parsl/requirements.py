"""Canonical worker requirements for Parsl executor routing."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
from typing import Any
from urllib.parse import unquote, urlsplit

from bioimageflow.cache import compute_env_hash
from bioimageflow.resources import parse_capacity
from bioimageflow_core import EnvironmentSpec, ProcessingTool, ResourceSpec
from bioimageflow_core.worker_origins import (
    WorkerToolOriginV1,
    decode_worker_tool_origin,
    encode_worker_tool_origin,
)


_CORE_REQUIREMENT_PATTERN = re.compile(
    r"^bioimageflow[-_.]core(?P<constraint>[<>=!~].+)$",
    re.IGNORECASE,
)


class WorkerRequirementError(ValueError):
    """A processing tool cannot be represented as a portable worker requirement."""


def parse_memory_bytes(value: str, *, field: str = "memory") -> int:
    """Parse one canonical integral byte-capacity string."""
    try:
        return parse_capacity(value, field=field)
    except TypeError:
        raise
    except ValueError as exc:
        raise WorkerRequirementError(str(exc)) from exc


def normalize_core_requirement(value: str) -> str:
    """Return the conservative canonical BioImageFlow core requirement."""
    if type(value) is not str:
        raise TypeError("core_requirement must be a string.")
    compact = "".join(value.split())
    match = _CORE_REQUIREMENT_PATTERN.fullmatch(compact)
    if match is None:
        raise WorkerRequirementError(
            "core_requirement must constrain the bioimageflow-core distribution."
        )
    return f"bioimageflow-core{match.group('constraint')}"


def canonical_environment_identity(
    *,
    name: str,
    dependency_hash: str,
    allow_flexible_versions: bool,
) -> str:
    """Return the stable route key for one complete environment identity."""
    if type(name) is not str or not name or name != name.strip():
        raise WorkerRequirementError(
            "Environment name must be a non-empty, trimmed string."
        )
    if (
        type(dependency_hash) is not str
        or re.fullmatch(r"[0-9a-f]{64}", dependency_hash) is None
    ):
        raise WorkerRequirementError(
            "Environment dependency hash must be a lowercase SHA-256 digest."
        )
    if type(allow_flexible_versions) is not bool:
        raise TypeError("allow_flexible_versions must be a boolean.")
    material = json.dumps(
        {
            "allow_flexible_versions": allow_flexible_versions,
            "dependency_hash": dependency_hash,
            "name": name,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return f"env_{hashlib.sha256(material.encode()).hexdigest()}"


@dataclass(frozen=True, slots=True)
class NormalizedResourceRequest:
    """Validated resource request used only for executor compatibility."""

    cpu: int = 1
    gpu: int = 0
    memory_bytes: int | None = None
    gpu_memory_bytes: int | None = None
    max_concurrent: int = 0

    def __post_init__(self) -> None:
        if type(self.cpu) is not int or self.cpu < 1:
            raise WorkerRequirementError("cpu must be a positive integer.")
        if type(self.gpu) is not int or self.gpu < 0:
            raise WorkerRequirementError("gpu must be a non-negative integer.")
        for field, value in (
            ("memory_bytes", self.memory_bytes),
            ("gpu_memory_bytes", self.gpu_memory_bytes),
        ):
            if value is not None and (type(value) is not int or value < 1):
                raise WorkerRequirementError(
                    f"{field} must be a positive integer or None."
                )
        if type(self.max_concurrent) is not int or self.max_concurrent < 0:
            raise WorkerRequirementError(
                "max_concurrent must be a non-negative integer."
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "cpu": self.cpu,
            "gpu": self.gpu,
            "memory_bytes": self.memory_bytes,
            "gpu_memory_bytes": self.gpu_memory_bytes,
            "max_concurrent": self.max_concurrent,
        }

    @classmethod
    def from_dict(cls, value: Any) -> "NormalizedResourceRequest":
        fields = {
            "cpu",
            "gpu",
            "memory_bytes",
            "gpu_memory_bytes",
            "max_concurrent",
        }
        if not isinstance(value, dict) or set(value) != fields:
            raise ValueError("Invalid NormalizedResourceRequest payload.")
        return cls(**value)


def normalize_resource_request(
    resources: ResourceSpec | None,
) -> NormalizedResourceRequest:
    """Validate a tool ResourceSpec and normalize memory values to bytes."""
    selected = resources or ResourceSpec()
    if type(selected) is not ResourceSpec:
        raise TypeError("ProcessingTool.resources must be ResourceSpec or None.")
    if type(selected.cpu) is not int or selected.cpu < 1:
        raise WorkerRequirementError("ResourceSpec.cpu must be a positive integer.")
    if type(selected.gpu) is not int or selected.gpu < 0:
        raise WorkerRequirementError(
            "ResourceSpec.gpu must be a non-negative integer."
        )
    if type(selected.max_concurrent) is not int or selected.max_concurrent < 0:
        raise WorkerRequirementError(
            "ResourceSpec.max_concurrent must be a non-negative integer."
        )
    memory_bytes = (
        None
        if selected.memory is None
        else parse_memory_bytes(selected.memory, field="ResourceSpec.memory")
    )
    gpu_memory_bytes = (
        None
        if selected.gpu_memory is None
        else parse_memory_bytes(
            selected.gpu_memory,
            field="ResourceSpec.gpu_memory",
        )
    )
    return NormalizedResourceRequest(
        cpu=selected.cpu,
        gpu=selected.gpu,
        memory_bytes=memory_bytes,
        gpu_memory_bytes=gpu_memory_bytes,
        max_concurrent=selected.max_concurrent,
    )


def _normalized_absolute_path(value: Any, *, field: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise WorkerRequirementError(f"{field} must be a non-empty path string.")
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise WorkerRequirementError(f"{field} must be an anchored absolute path.")
    return str(path.resolve(strict=False))


def _file_uri_path(value: str) -> str | None:
    direct_reference = value.split("@", 1)[1].strip() if "@" in value else value
    if not direct_reference.startswith("file:"):
        return None
    parsed = urlsplit(direct_reference)
    if parsed.scheme != "file" or parsed.netloc not in {"", "localhost"}:
        raise WorkerRequirementError(
            f"Unsupported local dependency URI {direct_reference!r}."
        )
    if parsed.query or parsed.fragment:
        raise WorkerRequirementError(
            f"Local dependency URI must not contain query or fragment data: "
            f"{direct_reference!r}."
        )
    return _normalized_absolute_path(
        unquote(parsed.path),
        field="Local dependency URI",
    )


def anchored_dependency_paths(dependencies: Mapping[str, Any]) -> tuple[str, ...]:
    """Extract normalized local paths that executor preflight must prove shared."""
    if not isinstance(dependencies, Mapping):
        raise TypeError("Environment dependencies must be a mapping.")
    paths: set[str] = set()

    def visit(value: Any) -> None:
        if isinstance(value, Mapping):
            if "path" in value:
                paths.add(
                    _normalized_absolute_path(
                        value["path"],
                        field="Local dependency path",
                    )
                )
            for child in value.values():
                visit(child)
            return
        if isinstance(value, (list, tuple)):
            for child in value:
                visit(child)
            return
        if type(value) is str:
            path = _file_uri_path(value)
            if path is not None:
                paths.add(path)

    visit(dependencies)
    return tuple(sorted(paths))


def _validate_wetlands_settings(
    workflow_environment: Any | None,
    *,
    environment_name: str,
) -> None:
    if workflow_environment is None:
        return
    configured_name = getattr(workflow_environment, "name", None)
    if configured_name != environment_name:
        raise WorkerRequirementError(
            f"Workflow environment {configured_name!r} does not configure "
            f"{environment_name!r}."
        )
    configured: list[str] = []
    if getattr(workflow_environment, "max_workers", 0) != 0:
        configured.append("max_workers")
    if getattr(workflow_environment, "worker_env", None) is not None:
        configured.append("worker_env")
    if getattr(workflow_environment, "worker_timeout", None) is not None:
        configured.append("worker_timeout")
    if configured:
        raise WorkerRequirementError(
            f"Parsl environment {environment_name!r} cannot use Wetlands-only "
            f"settings {configured}."
        )


@dataclass(frozen=True, slots=True)
class WorkerRequirement:
    """Complete immutable requirement for one scoped ProcessingTool node."""

    scoped_node_name: str
    environment_name: str
    dependency_hash: str
    allow_flexible_versions: bool
    core_requirement: str
    anchored_dependency_paths: tuple[str, ...]
    resources: NormalizedResourceRequest
    tool_origin: WorkerToolOriginV1

    def __post_init__(self) -> None:
        if (
            type(self.scoped_node_name) is not str
            or not self.scoped_node_name
            or self.scoped_node_name != self.scoped_node_name.strip()
        ):
            raise WorkerRequirementError(
                "scoped_node_name must be a non-empty, trimmed string."
            )
        canonical_environment_identity(
            name=self.environment_name,
            dependency_hash=self.dependency_hash,
            allow_flexible_versions=self.allow_flexible_versions,
        )
        if normalize_core_requirement(self.core_requirement) != self.core_requirement:
            raise WorkerRequirementError(
                "core_requirement must use canonical normalized spelling."
            )
        if type(self.anchored_dependency_paths) is not tuple:
            raise TypeError("anchored_dependency_paths must be a tuple.")
        normalized_paths = tuple(
            _normalized_absolute_path(path, field="Anchored dependency path")
            for path in self.anchored_dependency_paths
        )
        if (
            normalized_paths != self.anchored_dependency_paths
            or tuple(sorted(set(normalized_paths))) != normalized_paths
        ):
            raise WorkerRequirementError(
                "anchored_dependency_paths must be sorted, unique, and normalized."
            )
        if type(self.resources) is not NormalizedResourceRequest:
            raise TypeError("resources must be a NormalizedResourceRequest.")
        if (
            decode_worker_tool_origin(
                encode_worker_tool_origin(self.tool_origin)
            )
            != self.tool_origin
        ):
            raise WorkerRequirementError("tool_origin must be canonical.")

    @property
    def environment_identity(self) -> str:
        return canonical_environment_identity(
            name=self.environment_name,
            dependency_hash=self.dependency_hash,
            allow_flexible_versions=self.allow_flexible_versions,
        )

    @property
    def tool_origin_mode(self) -> str:
        return self.tool_origin.kind


def build_worker_requirement(
    scoped_node_name: str,
    tool: ProcessingTool,
    tool_origin: WorkerToolOriginV1,
    *,
    core_requirement: str,
    workflow_environment: Any | None = None,
    resources: ResourceSpec | None = None,
) -> WorkerRequirement:
    """Build and validate one canonical requirement before DFK acquisition."""
    if (
        type(scoped_node_name) is not str
        or not scoped_node_name
        or scoped_node_name != scoped_node_name.strip()
    ):
        raise WorkerRequirementError(
            "scoped_node_name must be a non-empty, trimmed string."
        )
    if not isinstance(tool, ProcessingTool):
        raise TypeError("tool must be a ProcessingTool.")
    environment = getattr(tool, "environment", None)
    if type(environment) is not EnvironmentSpec:
        raise WorkerRequirementError(
            f"Processing tool {type(tool).__name__!r} must declare EnvironmentSpec."
        )
    _validate_wetlands_settings(
        workflow_environment,
        environment_name=environment.name,
    )
    canonical_origin = decode_worker_tool_origin(
        encode_worker_tool_origin(tool_origin)
    )
    dependency_hash = compute_env_hash(environment.dependencies)
    canonical_environment_identity(
        name=environment.name,
        dependency_hash=dependency_hash,
        allow_flexible_versions=environment.allow_flexible_versions,
    )
    return WorkerRequirement(
        scoped_node_name=scoped_node_name,
        environment_name=environment.name,
        dependency_hash=dependency_hash,
        allow_flexible_versions=environment.allow_flexible_versions,
        core_requirement=normalize_core_requirement(core_requirement),
        anchored_dependency_paths=anchored_dependency_paths(
            environment.dependencies
        ),
        resources=normalize_resource_request(
            resources if resources is not None else getattr(tool, "resources", None)
        ),
        tool_origin=canonical_origin,
    )


__all__ = [
    "NormalizedResourceRequest",
    "WorkerRequirement",
    "WorkerRequirementError",
    "anchored_dependency_paths",
    "build_worker_requirement",
    "canonical_environment_identity",
    "normalize_core_requirement",
    "normalize_resource_request",
    "parse_memory_bytes",
]
