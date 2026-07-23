"""Lazy boundary for the external Parsl dependency."""

from __future__ import annotations

from importlib import import_module
from types import ModuleType


def require_parsl() -> ModuleType:
    """Load Parsl after the caller has selected a Parsl operation."""
    try:
        return import_module("parsl")
    except ModuleNotFoundError as exc:
        if exc.name != "parsl":
            raise
        raise ImportError(
            "The optional dependency 'parsl' is required for Parsl execution. "
            "Install it with 'bioimageflow[parsl]'."
        ) from exc


__all__: list[str] = []
