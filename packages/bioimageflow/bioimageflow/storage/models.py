"""Storage errors and immutable public value types."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Mapping

from bioimageflow_core import ViewerSpec


class CacheCorruptionError(RuntimeError):
    """Raised when cache metadata points to corrupt or unsafe state."""


@dataclass(frozen=True)
class OutputViewCapability:
    """Structured result from probing one output-view materialization mode."""

    mode: str
    supported: bool
    code: Literal[
        "ok",
        "permission_denied",
        "filesystem_unsupported",
        "invalid_mode",
        "io_error",
    ]
    detail: str | None = None


@dataclass(frozen=True)
class OutputViewerMetadata:
    """Typed retained viewer metadata for one produced output column."""

    output: str
    viewer: ViewerSpec


@dataclass(frozen=True)
class RunNodeResult:
    """Public typed lookup result for one retained run/node selection."""

    run_id: str
    node_key: str
    result_key: str
    record_id: str
    cache_hit: bool
    canonical: str
    outputs: tuple[Mapping[str, Any], ...]
    provenance: Mapping[str, Any] | None = None
    viewers: tuple[OutputViewerMetadata, ...] = ()
