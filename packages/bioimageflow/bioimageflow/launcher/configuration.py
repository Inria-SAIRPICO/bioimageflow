"""Trusted import and secret resolution for submitted Parsl configuration."""

from __future__ import annotations

import importlib
import os
from collections.abc import Callable, Collection
from typing import Any

from .errors import LauncherProtocolError
from .types import ParslConfigRef


SecretResolver = Callable[[str], str]


def import_config_factory(
    reference: str,
    *,
    trusted_factories: Collection[str] | None = None,
) -> Callable[..., Any]:
    """Resolve an importable configuration factory without invoking it."""
    if trusted_factories is not None and reference not in trusted_factories:
        raise LauncherProtocolError(
            f"Parsl configuration factory {reference!r} is not trusted.",
            details={"factory": reference},
        )
    module_name, separator, qualname = reference.partition(":")
    if separator != ":" or not module_name or not qualname:
        raise LauncherProtocolError(
            "Parsl configuration factory must use 'module:callable' syntax."
        )
    try:
        value: Any = importlib.import_module(module_name)
        for part in qualname.split("."):
            if not part or part == "<locals>":
                raise AttributeError(part)
            value = getattr(value, part)
    except (ImportError, AttributeError) as exc:
        raise LauncherProtocolError(
            f"Cannot import Parsl configuration factory {reference!r}.",
            details={"factory": reference},
        ) from exc
    if not callable(value):
        raise LauncherProtocolError(
            f"Parsl configuration factory {reference!r} is not callable.",
            details={"factory": reference},
        )
    return value


def environment_secret_resolver(reference: str) -> str:
    """Resolve one opaque reference from the orchestrator environment."""
    try:
        return os.environ[reference]
    except KeyError as exc:
        raise LauncherProtocolError(
            f"Required secret reference {reference!r} is unavailable.",
            details={"secret_ref": reference},
        ) from exc


def verify_secret_references(
    reference: ParslConfigRef,
    *,
    resolver: SecretResolver = environment_secret_resolver,
) -> None:
    """Fail before launch when a host cannot resolve a required secret."""
    for opaque_name in (reference.secret_refs or {}).values():
        value = resolver(opaque_name)
        if type(value) is not str:
            raise LauncherProtocolError(
                f"Secret resolver returned a non-string for {opaque_name!r}.",
                details={"secret_ref": opaque_name},
            )


def build_parsl_config(
    reference: ParslConfigRef,
    *,
    resolver: SecretResolver = environment_secret_resolver,
    trusted_factories: Collection[str] | None = None,
) -> Any:
    """Invoke a trusted factory with JSON values and resolved secrets."""
    factory = import_config_factory(
        reference.factory,
        trusted_factories=trusted_factories,
    )
    kwargs = reference.to_dict()["kwargs"]
    for argument, opaque_name in (reference.secret_refs or {}).items():
        kwargs[argument] = resolver(opaque_name)
    try:
        return factory(**kwargs)
    except Exception:
        raise LauncherProtocolError(
            f"Parsl configuration factory {reference.factory!r} failed.",
            details={"factory": reference.factory},
        ) from None
