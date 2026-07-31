"""Portable node-instance worker resource requirements."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, ClassVar

from bioimageflow_core import ResourceSpec

_CAPACITY_PATTERN = re.compile(
    r"^(?P<amount>[1-9][0-9]*)(?P<unit>B|KB|MB|GB|TB|KiB|MiB|GiB|TiB)$"
)
_CAPACITY_MULTIPLIERS = {
    "B": 1,
    "KB": 1024,
    "MB": 1024**2,
    "GB": 1024**3,
    "TB": 1024**4,
    "KiB": 1024,
    "MiB": 1024**2,
    "GiB": 1024**3,
    "TiB": 1024**4,
}


def parse_capacity(value: str, *, field: str) -> int:
    if type(value) is not str:
        raise TypeError(f"{field} must be a string.")
    match = _CAPACITY_PATTERN.fullmatch(value)
    if match is None:
        raise ValueError(
            f"{field} must be an integral capacity with unit B, KB, MB, GB, TB, "
            "KiB, MiB, GiB, or TiB."
        )
    return int(match.group("amount")) * _CAPACITY_MULTIPLIERS[match.group("unit")]


def _validate_resource_spec(value: ResourceSpec) -> None:
    if type(value) is not ResourceSpec:
        raise TypeError("Tool resources must be ResourceSpec.")
    if type(value.cpu) is not int or value.cpu < 1:
        raise ValueError("ResourceSpec.cpu must be a positive integer.")
    if type(value.gpu) is not int or value.gpu < 0:
        raise ValueError("ResourceSpec.gpu must be a non-negative integer.")
    if type(value.max_concurrent) is not int or value.max_concurrent < 0:
        raise ValueError("ResourceSpec.max_concurrent must be non-negative.")
    if value.memory is not None:
        parse_capacity(value.memory, field="ResourceSpec.memory")
    if value.gpu_memory is not None:
        parse_capacity(value.gpu_memory, field="ResourceSpec.gpu_memory")


@dataclass(frozen=True, slots=True)
class NodeResourceOverrides:
    """Optional resource values attached to one ProcessingTool node."""

    SCHEMA: ClassVar[str] = "bioimageflow.node_resource_overrides.v1"

    cpu: int | None = None
    gpu: int | None = None
    memory: str | None = None
    gpu_memory: str | None = None
    max_concurrent: int | None = None

    def __post_init__(self) -> None:
        if self.cpu is not None and (type(self.cpu) is not int or self.cpu < 1):
            raise ValueError("cpu must be a positive integer or None.")
        if self.gpu is not None and (type(self.gpu) is not int or self.gpu < 0):
            raise ValueError("gpu must be a non-negative integer or None.")
        if self.max_concurrent is not None and (
            type(self.max_concurrent) is not int or self.max_concurrent < 0
        ):
            raise ValueError("max_concurrent must be non-negative or None.")
        if self.memory is not None:
            parse_capacity(self.memory, field="memory")
        if self.gpu_memory is not None:
            parse_capacity(self.gpu_memory, field="gpu_memory")

    def effective(self, declared: ResourceSpec | None) -> ResourceSpec:
        """Merge overrides with declaration floors and concurrency ceilings."""
        base = declared or ResourceSpec()
        _validate_resource_spec(base)
        cpu = base.cpu if self.cpu is None else self.cpu
        gpu = base.gpu if self.gpu is None else self.gpu
        memory = base.memory if self.memory is None else self.memory
        gpu_memory = base.gpu_memory if self.gpu_memory is None else self.gpu_memory
        if cpu < base.cpu:
            raise ValueError(f"cpu override {cpu} is below tool floor {base.cpu}.")
        if gpu < base.gpu:
            raise ValueError(f"gpu override {gpu} is below tool floor {base.gpu}.")
        if (
            base.memory is not None
            and memory is not None
            and parse_capacity(memory, field="memory")
            < parse_capacity(base.memory, field="declared memory")
        ):
            raise ValueError("memory override is below the tool declaration.")
        if (
            base.gpu_memory is not None
            and gpu_memory is not None
            and parse_capacity(gpu_memory, field="gpu_memory")
            < parse_capacity(base.gpu_memory, field="declared gpu_memory")
        ):
            raise ValueError("gpu_memory override is below the tool declaration.")
        override_cap = self.max_concurrent
        declared_cap = base.max_concurrent
        if declared_cap and override_cap and override_cap > declared_cap:
            raise ValueError(
                f"max_concurrent override {override_cap} exceeds tool cap {declared_cap}."
            )
        if override_cap is None:
            max_concurrent = declared_cap
        elif declared_cap == 0:
            max_concurrent = override_cap
        elif override_cap == 0:
            raise ValueError(
                "max_concurrent=0 cannot remove a finite tool concurrency cap."
            )
        else:
            max_concurrent = min(declared_cap, override_cap)
        return ResourceSpec(
            cpu=cpu,
            gpu=gpu,
            memory=memory,
            gpu_memory=gpu_memory,
            max_concurrent=max_concurrent,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "cpu": self.cpu,
            "gpu": self.gpu,
            "memory": self.memory,
            "gpu_memory": self.gpu_memory,
            "max_concurrent": self.max_concurrent,
        }

    @classmethod
    def from_dict(cls, value: Any) -> "NodeResourceOverrides":
        if not isinstance(value, dict):
            raise TypeError("NodeResourceOverrides must be an object.")
        expected = {
            "schema",
            "cpu",
            "gpu",
            "memory",
            "gpu_memory",
            "max_concurrent",
        }
        if set(value) != expected or value["schema"] != cls.SCHEMA:
            raise ValueError("Invalid NodeResourceOverrides payload.")
        return cls(
            cpu=value["cpu"],
            gpu=value["gpu"],
            memory=value["memory"],
            gpu_memory=value["gpu_memory"],
            max_concurrent=value["max_concurrent"],
        )


def effective_node_resources(node: Any) -> ResourceSpec:
    """Return the validated effective resources for a processing node."""
    from bioimageflow.node import Node
    from bioimageflow_core import ProcessingTool

    if not isinstance(node, Node):
        raise TypeError("node must be a Node.")
    if not isinstance(node.tool, ProcessingTool):
        raise TypeError("Worker resources apply only to ProcessingTool nodes.")
    overrides = node.resource_overrides
    declared = getattr(node.tool, "resources", None)
    if overrides is None:
        base = declared or ResourceSpec()
        _validate_resource_spec(base)
        return base
    return overrides.effective(declared)


__all__ = ["NodeResourceOverrides", "effective_node_resources", "parse_capacity"]
