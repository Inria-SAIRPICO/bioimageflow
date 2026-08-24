"""Frozen public values for managed remote-cluster execution."""

from __future__ import annotations

import math
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path, PurePosixPath
from typing import Any, ClassVar, Literal, cast

from bioimageflow.parsl.requirements import parse_memory_bytes

from ._common import (
    IDENTIFIER_RE,
    MODULE_FACTORY_RE,
    exact_dict,
    freeze_json,
    normalized_cluster_path,
    positive_number,
    thaw_json,
    validate_host,
    validate_reference_mapping,
)
from ._setup_script import SetupScript


EnvironmentKind = Literal["uv", "pixi", "pylock", "wheelhouse", "existing_python"]


def _path(value: object, *, field_name: str) -> Path:
    if not isinstance(value, (str, os.PathLike)):
        raise TypeError(f"{field_name} must be path-like.")
    return Path(value).expanduser().absolute()


def _string_tuple(value: Sequence[str], *, field_name: str) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise TypeError(f"{field_name} must be a sequence of strings.")
    result = tuple(value)
    if any(
        type(item) is not str or not item or item != item.strip() for item in result
    ):
        raise ValueError(f"{field_name} must contain non-empty trimmed strings.")
    if len(set(result)) != len(result):
        raise ValueError(f"{field_name} must not contain duplicates.")
    return result


def _optional_trimmed(value: str | None, *, field_name: str) -> str | None:
    if value is None:
        return None
    if type(value) is not str or not value or value != value.strip():
        raise ValueError(f"{field_name} must be a non-empty trimmed string or None.")
    if any(ord(character) < 32 for character in value):
        raise ValueError(f"{field_name} must not contain control characters.")
    return value


@dataclass(frozen=True, slots=True, repr=False)
class ClusterEnvironment:
    """A tagged, reproducible source for the cluster Python environment."""

    SCHEMA: ClassVar[str] = "bioimageflow.cluster_environment.v1"

    kind: EnvironmentKind
    source: Path | PurePosixPath
    lock: Path | None = None
    project: Path | None = None
    groups: tuple[str, ...] = ()
    extras: tuple[str, ...] = ()
    package: str | None = None
    environment: str | None = None
    auth_refs: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.kind not in {"uv", "pixi", "pylock", "wheelhouse", "existing_python"}:
            raise ValueError("Unknown cluster environment kind.")
        object.__setattr__(
            self, "groups", _string_tuple(self.groups, field_name="groups")
        )
        object.__setattr__(
            self, "extras", _string_tuple(self.extras, field_name="extras")
        )
        object.__setattr__(
            self,
            "auth_refs",
            validate_reference_mapping(self.auth_refs, field="auth_refs"),
        )
        object.__setattr__(
            self,
            "package",
            _optional_trimmed(self.package, field_name="package"),
        )
        object.__setattr__(
            self,
            "environment",
            _optional_trimmed(self.environment, field_name="environment"),
        )
        if self.kind == "existing_python":
            object.__setattr__(
                self,
                "source",
                normalized_cluster_path(self.source, field="path"),
            )
            if any(
                (
                    self.lock,
                    self.project,
                    self.groups,
                    self.extras,
                    self.package,
                    self.environment,
                    self.auth_refs,
                )
            ):
                raise ValueError(
                    "Existing Python accepts only its absolute cluster path."
                )
        else:
            object.__setattr__(self, "source", _path(self.source, field_name="source"))
            if self.lock is not None:
                object.__setattr__(self, "lock", _path(self.lock, field_name="lock"))
            if self.project is not None:
                object.__setattr__(
                    self, "project", _path(self.project, field_name="project")
                )
        if self.kind == "uv" and any((self.lock, self.project, self.environment)):
            raise ValueError(
                "A uv environment accepts only groups, extras, package, and auth_refs."
            )
        if self.kind == "pixi" and any(
            (self.lock, self.project, self.groups, self.extras, self.package)
        ):
            raise ValueError(
                "A Pixi environment accepts only environment and auth_refs."
            )
        if self.kind == "pixi" and self.environment is None:
            raise ValueError("A Pixi environment requires a named environment.")
        if self.kind == "pylock":
            if self.lock is None or self.source != self.lock:
                raise ValueError(
                    "A pylock environment requires one matching lock path."
                )
            if self.package is not None or self.environment is not None:
                raise ValueError(
                    "A pylock environment does not accept package or environment."
                )
        if self.kind == "wheelhouse":
            if self.lock is None:
                raise ValueError("A wheelhouse environment requires a lock path.")
            if any(
                (
                    self.project,
                    self.groups,
                    self.extras,
                    self.package,
                    self.environment,
                    self.auth_refs,
                )
            ):
                raise ValueError(
                    "A wheelhouse environment accepts only its path and lock."
                )

    @classmethod
    def from_uv_project(
        cls,
        path: str | Path,
        *,
        groups: Sequence[str] = (),
        extras: Sequence[str] = (),
        package: str | None = None,
        auth_refs: Mapping[str, str] | None = None,
    ) -> "ClusterEnvironment":
        return cls(
            "uv",
            _path(path, field_name="path"),
            groups=tuple(groups),
            extras=tuple(extras),
            package=package,
            auth_refs=auth_refs or {},
        )

    @classmethod
    def from_pixi_project(
        cls,
        path: str | Path,
        *,
        environment: str = "default",
        auth_refs: Mapping[str, str] | None = None,
    ) -> "ClusterEnvironment":
        return cls(
            "pixi",
            _path(path, field_name="path"),
            environment=environment,
            auth_refs=auth_refs or {},
        )

    @classmethod
    def from_pylock(
        cls,
        lock: str | Path,
        *,
        project: str | Path | None = None,
        groups: Sequence[str] = (),
        extras: Sequence[str] = (),
        auth_refs: Mapping[str, str] | None = None,
    ) -> "ClusterEnvironment":
        source = _path(lock, field_name="lock")
        return cls(
            "pylock",
            source,
            lock=source,
            project=None if project is None else _path(project, field_name="project"),
            groups=tuple(groups),
            extras=tuple(extras),
            auth_refs=auth_refs or {},
        )

    @classmethod
    def from_wheelhouse(
        cls,
        path: str | Path,
        *,
        lock: str | Path,
    ) -> "ClusterEnvironment":
        return cls(
            "wheelhouse",
            _path(path, field_name="path"),
            lock=_path(lock, field_name="lock"),
        )

    @classmethod
    def from_existing_python(cls, path: str | PurePosixPath) -> "ClusterEnvironment":
        return cls("existing_python", normalized_cluster_path(path, field="path"))

    @property
    def ownership(self) -> Literal["content", "external"]:
        return "external" if self.kind == "existing_python" else "content"

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "kind": self.kind,
            "source": str(self.source),
            "lock": None if self.lock is None else str(self.lock),
            "project": None if self.project is None else str(self.project),
            "groups": list(self.groups),
            "extras": list(self.extras),
            "package": self.package,
            "environment": self.environment,
            "auth_refs": dict(self.auth_refs),
        }

    @classmethod
    def from_dict(cls, value: Any) -> "ClusterEnvironment":
        data = exact_dict(
            value,
            {
                "schema",
                "kind",
                "source",
                "lock",
                "project",
                "groups",
                "extras",
                "package",
                "environment",
                "auth_refs",
            },
            cls.__name__,
        )
        if data["schema"] != cls.SCHEMA:
            raise ValueError("Unsupported ClusterEnvironment schema.")
        if type(data["kind"]) is not str or type(data["source"]) is not str:
            raise ValueError("Invalid ClusterEnvironment kind or source.")
        if type(data["groups"]) is not list or type(data["extras"]) is not list:
            raise ValueError("ClusterEnvironment groups and extras must be arrays.")
        if data["lock"] is not None and type(data["lock"]) is not str:
            raise ValueError("ClusterEnvironment lock must be a path string or null.")
        if data["project"] is not None and type(data["project"]) is not str:
            raise ValueError(
                "ClusterEnvironment project must be a path string or null."
            )
        return cls(
            kind=cast(EnvironmentKind, data["kind"]),
            source=PurePosixPath(data["source"])
            if data["kind"] == "existing_python"
            else Path(data["source"]),
            lock=None if data["lock"] is None else Path(data["lock"]),
            project=None if data["project"] is None else Path(data["project"]),
            groups=tuple(data["groups"]),
            extras=tuple(data["extras"]),
            package=data["package"],
            environment=data["environment"],
            auth_refs=data["auth_refs"],
        )


@dataclass(frozen=True, slots=True)
class SchedulerJob:
    """Normalized scheduler request for the BioImageFlow orchestrator only."""

    SCHEMA: ClassVar[str] = "bioimageflow.scheduler_job.v1"
    scheduler: Literal["slurm", "pbs", "lsf"]
    walltime: timedelta
    queue: str | None = None
    project: str | None = None
    cpu: int = 1
    memory: str | int | None = None
    gpu: int = 0
    attributes: Mapping[str, Any] = field(default_factory=dict)
    hard_cancel_after: float | None = None

    def __post_init__(self) -> None:
        if self.scheduler not in {"slurm", "pbs", "lsf"}:
            raise ValueError("scheduler must be 'slurm', 'pbs', or 'lsf'.")
        if type(self.walltime) is not timedelta:
            raise TypeError("walltime must be a datetime.timedelta.")
        seconds = self.walltime.total_seconds()
        if not math.isfinite(seconds) or seconds <= 0 or not seconds.is_integer():
            raise ValueError(
                "walltime must be a positive datetime.timedelta resolving to whole seconds."
            )
        for name, value in (("queue", self.queue), ("project", self.project)):
            if value is not None and (
                type(value) is not str
                or not value
                or value != value.strip()
                or any(
                    character.isspace() or ord(character) < 32 for character in value
                )
            ):
                raise ValueError(f"{name} must be a safe scheduler identifier.")
        if type(self.cpu) is not int or self.cpu <= 0:
            raise ValueError("cpu must be a positive integer.")
        if type(self.gpu) is not int or self.gpu < 0:
            raise ValueError("gpu must be a non-negative integer.")
        if self.memory is not None:
            if type(self.memory) is int:
                memory_bytes = self.memory
            elif type(self.memory) is str:
                memory_bytes = parse_memory_bytes(
                    "".join(self.memory.split()),
                    field="memory",
                )
            else:
                raise TypeError("memory must be a byte string, integer, or None.")
            if type(memory_bytes) is not int or memory_bytes <= 0:
                raise ValueError("memory must be a positive byte count.")
            object.__setattr__(self, "memory", memory_bytes)
        frozen = freeze_json(self.attributes, path="attributes")
        if not isinstance(frozen, Mapping):
            raise TypeError("attributes must be a mapping.")
        if any(isinstance(value, (Mapping, tuple)) for value in frozen.values()):
            raise ValueError(
                "Scheduler attributes must contain JSON scalar values only."
            )
        object.__setattr__(self, "attributes", frozen)
        if self.hard_cancel_after is not None:
            object.__setattr__(
                self,
                "hard_cancel_after",
                positive_number(self.hard_cancel_after, field="hard_cancel_after"),
            )

    @property
    def walltime_seconds(self) -> int:
        seconds = self.walltime.total_seconds()
        return int(seconds)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "scheduler": self.scheduler,
            "walltime_seconds": self.walltime_seconds,
            "queue": self.queue,
            "project": self.project,
            "cpu": self.cpu,
            "memory_bytes": self.memory,
            "gpu": self.gpu,
            "attributes": thaw_json(self.attributes),
            "hard_cancel_after_seconds": self.hard_cancel_after,
        }

    @classmethod
    def from_dict(cls, value: Any) -> "SchedulerJob":
        data = exact_dict(
            value,
            {
                "schema",
                "scheduler",
                "walltime_seconds",
                "queue",
                "project",
                "cpu",
                "memory_bytes",
                "gpu",
                "attributes",
                "hard_cancel_after_seconds",
            },
            cls.__name__,
        )
        if data["schema"] != cls.SCHEMA:
            raise ValueError("Unsupported SchedulerJob schema.")
        return cls(
            scheduler=data["scheduler"],
            walltime=timedelta(
                seconds=positive_number(
                    data["walltime_seconds"], field="walltime_seconds"
                )
            ),
            queue=data["queue"],
            project=data["project"],
            cpu=data["cpu"],
            memory=data["memory_bytes"],
            gpu=data["gpu"],
            attributes=data["attributes"],
            hard_cancel_after=data["hard_cancel_after_seconds"],
        )


@dataclass(frozen=True, slots=True)
class ParslConfiguration:
    """A local Parsl factory source or advanced installed module reference."""

    SCHEMA: ClassVar[str] = "bioimageflow.parsl_configuration.v1"

    source_kind: Literal["file", "module"]
    source: Path | str
    factory: str
    kwargs: Mapping[str, Any] = field(default_factory=dict)
    secret_refs: Mapping[str, str] = field(default_factory=dict)
    include: tuple[Path, ...] = ()

    def __post_init__(self) -> None:
        if self.source_kind == "file":
            object.__setattr__(self, "source", _path(self.source, field_name="path"))
            if (
                type(self.factory) is not str
                or IDENTIFIER_RE.fullmatch(self.factory) is None
            ):
                raise ValueError("factory must be one Python identifier.")
            includes = tuple(_path(item, field_name="include") for item in self.include)
            if len(set(includes)) != len(includes):
                raise ValueError("include must not contain duplicate paths.")
            object.__setattr__(self, "include", includes)
        elif self.source_kind == "module":
            if (
                type(self.source) is not str
                or MODULE_FACTORY_RE.fullmatch(self.source) is None
            ):
                raise ValueError(
                    "module source must be an importable module:function reference."
                )
            if self.factory != self.source.rsplit(":", 1)[1] or self.include:
                raise ValueError("Module configuration cannot carry file includes.")
        else:
            raise ValueError("Unknown Parsl configuration source kind.")
        frozen_kwargs = freeze_json(self.kwargs, path="kwargs")
        if not isinstance(frozen_kwargs, Mapping):
            raise TypeError("kwargs must be a mapping.")
        refs = validate_reference_mapping(self.secret_refs, field="secret_refs")
        overlap = set(frozen_kwargs) & set(refs)
        if overlap:
            raise ValueError(
                f"Arguments cannot occur in kwargs and secret_refs: {sorted(overlap)}."
            )
        object.__setattr__(self, "kwargs", frozen_kwargs)
        object.__setattr__(self, "secret_refs", refs)

    @classmethod
    def from_file(
        cls,
        path: str | Path,
        *,
        factory: str = "build",
        kwargs: Mapping[str, Any] | None = None,
        secret_refs: Mapping[str, str] | None = None,
        include: Sequence[str | Path] = (),
    ) -> "ParslConfiguration":
        return cls(
            "file",
            _path(path, field_name="path"),
            factory,
            kwargs or {},
            secret_refs or {},
            tuple(Path(item) for item in include),
        )

    @classmethod
    def from_module(
        cls,
        factory: str,
        *,
        kwargs: Mapping[str, Any] | None = None,
        secret_refs: Mapping[str, str] | None = None,
    ) -> "ParslConfiguration":
        if type(factory) is not str or MODULE_FACTORY_RE.fullmatch(factory) is None:
            raise ValueError("factory must be an importable module:function reference.")
        return cls(
            "module",
            factory,
            factory.rsplit(":", 1)[1],
            kwargs or {},
            secret_refs or {},
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "source_kind": self.source_kind,
            "source": str(self.source),
            "factory": self.factory,
            "kwargs": thaw_json(self.kwargs),
            "secret_refs": dict(self.secret_refs),
            "include": [str(item) for item in self.include],
        }

    @classmethod
    def from_dict(cls, value: Any) -> "ParslConfiguration":
        data = exact_dict(
            value,
            {
                "schema",
                "source_kind",
                "source",
                "factory",
                "kwargs",
                "secret_refs",
                "include",
            },
            cls.__name__,
        )
        if data["schema"] != cls.SCHEMA:
            raise ValueError("Unsupported ParslConfiguration schema.")
        return cls(
            source_kind=data["source_kind"],
            source=data["source"],
            factory=data["factory"],
            kwargs=data["kwargs"],
            secret_refs=data["secret_refs"],
            include=tuple(Path(item) for item in data["include"]),
        )


@dataclass(frozen=True, slots=True)
class RemoteClusterConfig:
    """Serializable configuration portion of :class:`RemoteCluster`."""

    SCHEMA: ClassVar[str] = "bioimageflow.remote_cluster.v1"

    host: str
    root: PurePosixPath
    environment: ClusterEnvironment | None = None
    parsl: ParslConfiguration | None = None
    orchestrator: SchedulerJob | None = None
    setup: SetupScript | None = None
    results_root: PurePosixPath | None = None
    connect_timeout: float = 15.0

    def __post_init__(self) -> None:
        object.__setattr__(self, "host", validate_host(self.host))
        object.__setattr__(
            self,
            "root",
            normalized_cluster_path(self.root, field="root", safe_token=True),
        )
        if self.results_root is not None:
            object.__setattr__(
                self,
                "results_root",
                normalized_cluster_path(self.results_root, field="results_root"),
            )
        timeout = positive_number(self.connect_timeout, field="connect_timeout")
        if timeout > 600:
            raise ValueError("connect_timeout must not exceed 600 seconds.")
        object.__setattr__(self, "connect_timeout", timeout)
        for name, value, expected in (
            ("environment", self.environment, ClusterEnvironment),
            ("parsl", self.parsl, ParslConfiguration),
            ("orchestrator", self.orchestrator, SchedulerJob),
            ("setup", self.setup, SetupScript),
        ):
            if value is not None and type(value) is not expected:
                raise TypeError(f"{name} must be {expected.__name__} or None.")

    @property
    def configured(self) -> bool:
        return (
            self.environment is not None
            and self.parsl is not None
            and self.orchestrator is not None
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "host": self.host,
            "root": str(self.root),
            "environment": None
            if self.environment is None
            else self.environment.to_dict(),
            "parsl": None if self.parsl is None else self.parsl.to_dict(),
            "orchestrator": None
            if self.orchestrator is None
            else self.orchestrator.to_dict(),
            "setup": None if self.setup is None else self.setup.to_dict(),
            "results_root": None
            if self.results_root is None
            else str(self.results_root),
            "connect_timeout": self.connect_timeout,
        }

    @classmethod
    def from_dict(cls, value: Any) -> "RemoteClusterConfig":
        data = exact_dict(
            value,
            {
                "schema",
                "host",
                "root",
                "environment",
                "parsl",
                "orchestrator",
                "setup",
                "results_root",
                "connect_timeout",
            },
            cls.__name__,
        )
        if data["schema"] != cls.SCHEMA:
            raise ValueError("Unsupported RemoteCluster schema.")
        return cls(
            host=data["host"],
            root=PurePosixPath(data["root"]),
            environment=None
            if data["environment"] is None
            else ClusterEnvironment.from_dict(data["environment"]),
            parsl=None
            if data["parsl"] is None
            else ParslConfiguration.from_dict(data["parsl"]),
            orchestrator=None
            if data["orchestrator"] is None
            else SchedulerJob.from_dict(data["orchestrator"]),
            setup=None
            if data["setup"] is None
            else SetupScript.from_dict(data["setup"]),
            results_root=None
            if data["results_root"] is None
            else PurePosixPath(data["results_root"]),
            connect_timeout=data["connect_timeout"],
        )


__all__ = [
    "ClusterEnvironment",
    "ParslConfiguration",
    "RemoteClusterConfig",
    "SchedulerJob",
    "SetupScript",
]
