"""Frozen JSON-safe configuration values for submitted execution."""

from __future__ import annotations

import copy
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from pathlib import PurePosixPath
from types import MappingProxyType
from typing import Any, Literal, cast


LauncherBackend = Literal["local", "manual"]
PSIJExecutor = Literal["slurm", "pbs", "lsf"]
_BACKENDS = frozenset({"local", "manual"})
_PSIJ_EXECUTORS = frozenset({"slurm", "pbs", "lsf"})
_SCHEDULER_VALUE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@/+-]*$")
_FACTORY_RE = re.compile(
    r"^[A-Za-z_][A-Za-z0-9_.]*:[A-Za-z_][A-Za-z0-9_.]*$"
)
_SECRET_KEY_PARTS = frozenset(
    {"api_key", "credential", "credentials", "password", "secret", "token"}
)


def _sensitive_key(key: str) -> bool:
    normalized = key.lower().replace("-", "_")
    return any(
        normalized == part
        or normalized.startswith(f"{part}_")
        or normalized.endswith(f"_{part}")
        for part in _SECRET_KEY_PARTS
    )


def _freeze_json(value: Any, *, path: str) -> Any:
    if value is None or type(value) in {bool, int, str}:
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError(f"{path} must not contain non-finite floats.")
        return value
    if type(value) in {list, tuple}:
        return tuple(
            _freeze_json(item, path=f"{path}[{index}]")
            for index, item in enumerate(value)
        )
    if isinstance(value, Mapping):
        frozen: dict[str, Any] = {}
        for key, item in value.items():
            if type(key) is not str or key == "":
                raise TypeError(f"{path} object keys must be non-empty strings.")
            if _sensitive_key(key):
                raise ValueError(
                    f"{path}.{key} looks secret-bearing; use secret_refs instead."
                )
            frozen[key] = _freeze_json(item, path=f"{path}.{key}")
        return MappingProxyType(frozen)
    raise TypeError(
        f"{path} contains non-JSON-safe value {type(value).__name__}."
    )


def _thaw_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return copy.deepcopy(value)


@dataclass(frozen=True, slots=True)
class ParslConfigRef:
    """Importable, JSON-safe Parsl configuration factory reference."""

    factory: str
    kwargs: Mapping[str, Any]
    secret_refs: Mapping[str, str] | None = None

    def __post_init__(self) -> None:
        if type(self.factory) is not str or _FACTORY_RE.fullmatch(self.factory) is None:
            raise ValueError(
                "factory must be an importable 'module:callable' reference."
            )
        if not isinstance(self.kwargs, Mapping):
            raise TypeError("kwargs must be a mapping.")
        frozen_kwargs = _freeze_json(self.kwargs, path="kwargs")
        assert isinstance(frozen_kwargs, Mapping)
        object.__setattr__(self, "kwargs", frozen_kwargs)

        refs = self.secret_refs
        if refs is None:
            object.__setattr__(self, "secret_refs", None)
            return
        if not isinstance(refs, Mapping):
            raise TypeError("secret_refs must be a mapping or None.")
        normalized: dict[str, str] = {}
        for argument, reference in refs.items():
            if (
                type(argument) is not str
                or argument == ""
                or type(reference) is not str
                or reference == ""
                or reference != reference.strip()
            ):
                raise ValueError(
                    "secret_refs keys and values must be non-empty trimmed strings."
                )
            if argument in self.kwargs:
                raise ValueError(
                    f"Secret argument {argument!r} must not also appear in kwargs."
                )
            normalized[argument] = reference
        object.__setattr__(self, "secret_refs", MappingProxyType(normalized))

    def to_dict(self) -> dict[str, Any]:
        return {
            "factory": self.factory,
            "kwargs": _thaw_json(self.kwargs),
            "secret_refs": (
                None
                if self.secret_refs is None
                else {key: value for key, value in self.secret_refs.items()}
            ),
        }

    @classmethod
    def from_dict(cls, value: Any) -> "ParslConfigRef":
        if not isinstance(value, dict) or set(value) != {
            "factory",
            "kwargs",
            "secret_refs",
        }:
            raise ValueError(
                "ParslConfigRef requires exactly factory, kwargs, and secret_refs."
            )
        return cls(
            factory=value["factory"],
            kwargs=value["kwargs"],
            secret_refs=value["secret_refs"],
        )


@dataclass(frozen=True, slots=True)
class OrchestratorLaunchConfig:
    """How the detached orchestrator process is started."""

    backend: LauncherBackend = "local"
    work_dir: Path | None = None
    hard_cancel_after: float | None = None

    def __post_init__(self) -> None:
        if type(self.backend) is not str or self.backend not in _BACKENDS:
            raise ValueError(
                f"Unknown launcher backend {self.backend!r}; expected one of "
                f"{sorted(_BACKENDS)}."
            )
        object.__setattr__(self, "backend", cast(LauncherBackend, self.backend))
        if self.work_dir is not None and not isinstance(
            self.work_dir, (str, Path)
        ):
            raise TypeError("work_dir must be path-like or None.")
        if self.hard_cancel_after is not None:
            if (
                type(self.hard_cancel_after) not in {int, float}
                or not math.isfinite(float(self.hard_cancel_after))
                or float(self.hard_cancel_after) <= 0
            ):
                raise ValueError("hard_cancel_after must be a positive finite number.")
            object.__setattr__(
                self,
                "hard_cancel_after",
                float(self.hard_cancel_after),
            )

    def normalized(self) -> "OrchestratorLaunchConfig":
        work_dir = self.work_dir
        if work_dir is not None:
            candidate = Path(work_dir).expanduser()
            work_dir = (
                candidate
                if candidate.is_absolute()
                else Path.cwd() / candidate
            ).resolve(strict=False)
        return OrchestratorLaunchConfig(
            backend=self.backend,
            work_dir=work_dir,
            hard_cancel_after=self.hard_cancel_after,
        )

    def to_dict(self) -> dict[str, Any]:
        normalized = self.normalized()
        return {
            "backend": normalized.backend,
            "work_dir": (
                None if normalized.work_dir is None else str(normalized.work_dir)
            ),
            "hard_cancel_after": normalized.hard_cancel_after,
        }

    @classmethod
    def from_dict(cls, value: Any) -> "OrchestratorLaunchConfig":
        if not isinstance(value, dict) or set(value) != {
            "backend",
            "work_dir",
            "hard_cancel_after",
        }:
            raise ValueError(
                "OrchestratorLaunchConfig requires exactly backend, work_dir, "
                "and hard_cancel_after."
            )
        return cls(
            backend=value["backend"],
            work_dir=None if value["work_dir"] is None else Path(value["work_dir"]),
            hard_cancel_after=value["hard_cancel_after"],
        ).normalized()


def _scheduler_value(value: object, *, field: str) -> str | None:
    if value is None:
        return None
    if (
        type(value) is not str
        or value != value.strip()
        or _SCHEDULER_VALUE_RE.fullmatch(value) is None
    ):
        raise ValueError(
            f"{field} must be a non-empty scheduler identifier without whitespace "
            "or shell syntax."
        )
    return value


def _positive_finite(value: object, *, field: str) -> float:
    if type(value) not in {int, float}:
        raise ValueError(f"{field} must be a positive finite number.")
    numeric = cast(float | int, value)
    if not math.isfinite(numeric) or numeric <= 0:
        raise ValueError(f"{field} must be a positive finite number.")
    return float(numeric)


def _cluster_work_dir(value: object) -> PurePosixPath | None:
    if value is None:
        return None
    if not isinstance(value, (str, PurePosixPath)):
        raise TypeError("work_dir must be a POSIX path-like value or None.")
    encoded = str(value)
    path = PurePosixPath(encoded)
    if (
        not encoded
        or "\x00" in encoded
        or not path.is_absolute()
        or encoded.startswith("//")
        or str(path) != encoded
        or any(part in {"", ".", ".."} for part in path.parts[1:])
    ):
        raise ValueError("work_dir must be a normalized absolute POSIX path.")
    return path


@dataclass(frozen=True, slots=True)
class PSIJLaunchConfig:
    """Strict scheduler configuration for one PSI/J orchestrator job."""

    executor: PSIJExecutor
    walltime: timedelta
    queue: str | None = None
    project: str | None = None
    cpu_cores: int = 1
    work_dir: PurePosixPath | None = None
    hard_cancel_after: float | None = None

    @property
    def backend(self) -> Literal["psij"]:
        return "psij"

    def __post_init__(self) -> None:
        if type(self.executor) is not str or self.executor not in _PSIJ_EXECUTORS:
            raise ValueError(
                f"Unknown PSI/J executor {self.executor!r}; expected one of "
                f"{sorted(_PSIJ_EXECUTORS)}."
            )
        object.__setattr__(self, "executor", cast(PSIJExecutor, self.executor))
        if type(self.walltime) is not timedelta:
            raise TypeError("walltime must be a datetime.timedelta.")
        _positive_finite(self.walltime.total_seconds(), field="walltime")
        object.__setattr__(
            self,
            "queue",
            _scheduler_value(self.queue, field="queue"),
        )
        object.__setattr__(
            self,
            "project",
            _scheduler_value(self.project, field="project"),
        )
        if type(self.cpu_cores) is not int or self.cpu_cores <= 0:
            raise ValueError("cpu_cores must be a positive integer.")
        object.__setattr__(self, "work_dir", _cluster_work_dir(self.work_dir))
        if self.hard_cancel_after is not None:
            object.__setattr__(
                self,
                "hard_cancel_after",
                _positive_finite(
                    self.hard_cancel_after,
                    field="hard_cancel_after",
                ),
            )

    def normalized(self) -> "PSIJLaunchConfig":
        return self

    def to_dict(self) -> dict[str, Any]:
        return {
            "backend": "psij",
            "executor": self.executor,
            "walltime_seconds": self.walltime.total_seconds(),
            "queue": self.queue,
            "project": self.project,
            "cpu_cores": self.cpu_cores,
            "work_dir": None if self.work_dir is None else str(self.work_dir),
            "hard_cancel_after": self.hard_cancel_after,
        }

    @classmethod
    def from_dict(cls, value: Any) -> "PSIJLaunchConfig":
        fields = {
            "backend",
            "executor",
            "walltime_seconds",
            "queue",
            "project",
            "cpu_cores",
            "work_dir",
            "hard_cancel_after",
        }
        if not isinstance(value, dict) or set(value) != fields:
            raise ValueError(
                "PSIJLaunchConfig requires exactly backend, executor, "
                "walltime_seconds, queue, project, cpu_cores, work_dir, and "
                "hard_cancel_after."
            )
        if value["backend"] != "psij":
            raise ValueError("PSIJLaunchConfig backend must be 'psij'.")
        seconds = _positive_finite(
            value["walltime_seconds"],
            field="walltime_seconds",
        )
        return cls(
            executor=value["executor"],
            walltime=timedelta(seconds=seconds),
            queue=value["queue"],
            project=value["project"],
            cpu_cores=value["cpu_cores"],
            work_dir=value["work_dir"],
            hard_cancel_after=value["hard_cancel_after"],
        )


LaunchConfig = OrchestratorLaunchConfig | PSIJLaunchConfig


def launch_config_from_dict(value: Any) -> LaunchConfig:
    """Decode exactly one local, manual, or PSI/J launch configuration."""
    if not isinstance(value, dict):
        raise ValueError("Launch configuration must be a JSON object.")
    backend = value.get("backend")
    if backend in _BACKENDS:
        return OrchestratorLaunchConfig.from_dict(value)
    if backend == "psij":
        return PSIJLaunchConfig.from_dict(value)
    raise ValueError(f"Unknown launcher backend {backend!r}.")
