"""Storage errors and immutable public value types."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


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
