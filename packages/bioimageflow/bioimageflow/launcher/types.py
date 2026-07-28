"""Frozen JSON-safe configuration values for submitted execution."""

from __future__ import annotations

import copy
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal, cast


LauncherBackend = Literal["local", "manual", "slurm", "pbs", "lsf", "oar"]
_BACKENDS = frozenset({"local", "manual", "slurm", "pbs", "lsf", "oar"})
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
