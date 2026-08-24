"""Shared validation and canonical encoding for managed cluster execution."""

from __future__ import annotations

import copy
import hashlib
import math
import re
from collections.abc import Mapping
from pathlib import PurePosixPath
from types import MappingProxyType
from typing import Any, cast

from bioimageflow.storage import canonical_json_bytes


DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
ENVIRONMENT_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
HOST_RE = re.compile(r"^(?:[A-Za-z0-9_.-]+@)?[A-Za-z0-9][A-Za-z0-9_.-]*$")
IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
MODULE_FACTORY_RE = re.compile(
    r"^[A-Za-z_][A-Za-z0-9_.]*:[A-Za-z_][A-Za-z0-9_]*$"
)
SAFE_ROOT_RE = re.compile(r"^/[A-Za-z0-9._/+:@-]+$")
SENSITIVE_PARTS = frozenset(
    {"api_key", "credential", "credentials", "password", "secret", "token"}
)


def canonical_digest(value: Any) -> str:
    return f"sha256:{hashlib.sha256(canonical_json_bytes(value)).hexdigest()}"


def exact_dict(value: Any, fields: set[str], name: str) -> dict[str, Any]:
    if type(value) is not dict or set(value) != fields:
        raise ValueError(f"Invalid {name} payload.")
    return value


def normalized_cluster_path(
    value: object,
    *,
    field: str,
    safe_token: bool = False,
) -> PurePosixPath:
    if not isinstance(value, (str, PurePosixPath)):
        raise TypeError(f"{field} must be a POSIX path-like value.")
    text = str(value)
    path = PurePosixPath(text)
    if (
        not text
        or any(character in text for character in ("\x00", "\n", "\r"))
        or not path.is_absolute()
        or text.startswith("//")
        or str(path) != text
        or any(part in {"", ".", ".."} for part in path.parts[1:])
        or (safe_token and SAFE_ROOT_RE.fullmatch(text) is None)
    ):
        suffix = " using only safe command-token characters" if safe_token else ""
        raise ValueError(f"{field} must be a normalized absolute POSIX path{suffix}.")
    return path


def validate_host(value: object) -> str:
    if (
        type(value) is not str
        or value.startswith("-")
        or "://" in value
        or HOST_RE.fullmatch(value) is None
    ):
        raise ValueError(
            "host must be one OpenSSH alias or user@host argument without options."
        )
    return value


def positive_number(value: object, *, field: str) -> float:
    if type(value) not in {int, float}:
        raise ValueError(f"{field} must be a positive finite number.")
    result = float(cast(int | float, value))
    if not math.isfinite(result):
        raise ValueError(f"{field} must be a positive finite number.")
    if result <= 0:
        raise ValueError(f"{field} must be a positive finite number.")
    return result


def sensitive_key(key: str) -> bool:
    normalized = key.lower().replace("-", "_")
    return any(
        normalized == part
        or normalized.startswith(f"{part}_")
        or normalized.endswith(f"_{part}")
        for part in SENSITIVE_PARTS
    )


def freeze_json(
    value: Any,
    *,
    path: str,
    reject_sensitive_keys: bool = True,
) -> Any:
    if value is None or type(value) in {bool, int, str}:
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError(f"{path} must not contain non-finite floats.")
        return value
    if type(value) in {list, tuple}:
        return tuple(
            freeze_json(
                item,
                path=f"{path}[{index}]",
                reject_sensitive_keys=reject_sensitive_keys,
            )
            for index, item in enumerate(value)
        )
    if isinstance(value, Mapping):
        frozen: dict[str, Any] = {}
        for key, item in value.items():
            if type(key) is not str or not key:
                raise TypeError(f"{path} object keys must be non-empty strings.")
            if reject_sensitive_keys and sensitive_key(key):
                raise ValueError(
                    f"{path}.{key} looks secret-bearing; use a secret reference."
                )
            frozen[key] = freeze_json(
                item,
                path=f"{path}.{key}",
                reject_sensitive_keys=reject_sensitive_keys,
            )
        return MappingProxyType(frozen)
    raise TypeError(f"{path} contains non-JSON-safe value {type(value).__name__}.")


def thaw_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [thaw_json(item) for item in value]
    return copy.deepcopy(value)


def validate_reference_mapping(
    value: Mapping[str, str] | None,
    *,
    field: str,
) -> Mapping[str, str]:
    if value is None:
        return MappingProxyType({})
    if not isinstance(value, Mapping):
        raise TypeError(f"{field} must be a mapping or None.")
    result: dict[str, str] = {}
    for key, reference in value.items():
        if type(key) is not str or IDENTIFIER_RE.fullmatch(key) is None:
            raise ValueError(f"{field} keys must be Python identifiers.")
        if type(reference) is not str or ENVIRONMENT_NAME_RE.fullmatch(reference) is None:
            raise ValueError(
                f"{field} values must be cluster environment-variable names."
            )
        result[key] = reference
    return MappingProxyType(result)


__all__ = [
    "DIGEST_RE",
    "IDENTIFIER_RE",
    "MODULE_FACTORY_RE",
    "canonical_digest",
    "exact_dict",
    "freeze_json",
    "normalized_cluster_path",
    "positive_number",
    "thaw_json",
    "validate_host",
    "validate_reference_mapping",
]
