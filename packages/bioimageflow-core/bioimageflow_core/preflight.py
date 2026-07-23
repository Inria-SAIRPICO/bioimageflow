"""Worker-safe executor preflight entry point."""

from __future__ import annotations

import importlib.metadata
import os
from pathlib import Path
import re
from typing import Any, Dict, List, Mapping, Sequence, Tuple

from bioimageflow_core.worker_origins import (
    WorkerToolOriginV1,
    _load_origin_class,
    decode_worker_tool_origin,
    worker_tool_origin_identity,
)
from bioimageflow_core.worker_protocol import TASK_SCHEMA


PREFLIGHT_SCHEMA = "bioimageflow.parsl.executor_preflight.v1"
PREFLIGHT_RESULT_SCHEMA = "bioimageflow.parsl.executor_preflight_result.v1"

_CORE_DISTRIBUTION = "bioimageflow-core"
_CORE_REQUIREMENT_RE = re.compile(
    r"^bioimageflow-core(?P<specifiers>.+)$"
)
_SPECIFIER_RE = re.compile(
    r"(?P<operator>===|==|!=|~=|<=|>=|<|>)"
    r"(?P<version>[0-9]+(?:\.[0-9]+)*(?:(?:a|b|rc)[0-9]+)?"
    r"(?:\.post[0-9]+)?(?:\.dev[0-9]+)?(?:\+[a-z0-9.-]+)?(?:\.\*)?)"
)
_VERSION_RE = re.compile(
    r"^(?P<release>[0-9]+(?:\.[0-9]+)*)"
    r"(?:(?P<pre_kind>a|b|rc)(?P<pre_number>[0-9]+))?"
    r"(?:\.post(?P<post>[0-9]+))?"
    r"(?:\.dev(?P<dev>[0-9]+))?"
    r"(?:\+[a-z0-9.-]+)?$"
)


def _exact_dict(
    value: Any,
    expected: Sequence[str],
    *,
    label: str,
) -> Dict[str, Any]:
    if type(value) is not dict:
        raise ValueError(f"{label} must be a plain object.")
    if not all(type(key) is str for key in value):
        raise ValueError(f"{label} keys must be strings.")
    actual = set(value)
    wanted = set(expected)
    if actual != wanted:
        raise ValueError(
            f"{label} fields do not match the schema; "
            f"missing={sorted(wanted - actual)}, extra={sorted(actual - wanted)}."
        )
    return value


def _text(value: Any, *, field: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ValueError(f"{field} must be a non-empty, trimmed string.")
    if "\x00" in value or any(ord(character) < 32 for character in value):
        raise ValueError(f"{field} contains invalid control characters.")
    return value


def _absolute_path(value: Any, *, field: str) -> str:
    text = _text(value, field=field)
    path = Path(text)
    if not path.is_absolute() or str(path.resolve(strict=False)) != text:
        raise ValueError(f"{field} must be an absolute normalized path.")
    return text


def _canonical_string_list(value: Any, *, field: str) -> Tuple[str, ...]:
    if type(value) is not list:
        raise ValueError(f"{field} must be a JSON array.")
    items = tuple(_text(item, field=field) for item in value)
    if not items:
        raise ValueError(f"{field} must not be empty.")
    if tuple(sorted(set(items))) != items:
        raise ValueError(f"{field} must be sorted and contain no duplicates.")
    return items


def _canonical_path_list(value: Any) -> Tuple[str, ...]:
    if type(value) is not list:
        raise ValueError("readable_paths must be a JSON array.")
    paths = tuple(
        _absolute_path(path, field="readable_paths item") for path in value
    )
    if not paths:
        raise ValueError("readable_paths must not be empty.")
    if tuple(sorted(set(paths))) != paths:
        raise ValueError(
            "readable_paths must be sorted and contain no duplicates."
        )
    return paths


def _origins(value: Any) -> Tuple[WorkerToolOriginV1, ...]:
    if type(value) is not list:
        raise ValueError("origins must be a JSON array.")
    decoded = tuple(decode_worker_tool_origin(item) for item in value)
    if not decoded:
        raise ValueError("origins must not be empty.")
    identities = tuple(worker_tool_origin_identity(origin) for origin in decoded)
    if tuple(sorted(set(identities))) != identities:
        raise ValueError(
            "origins must be sorted by identity and contain no duplicates."
        )
    return decoded


def _release_parts(value: str) -> Tuple[int, ...]:
    return tuple(int(part) for part in value.split("."))


def _version_parts(
    value: str,
) -> Tuple[Tuple[int, ...], Tuple[int, int], int, int]:
    match = _VERSION_RE.fullmatch(value)
    if match is None:
        raise ValueError(f"Unsupported core version {value!r}.")
    release = _release_parts(match.group("release"))
    pre_kind = match.group("pre_kind")
    dev = match.group("dev")
    post = match.group("post")
    if pre_kind is not None:
        pre_order = {"a": 0, "b": 1, "rc": 2}[pre_kind]
        phase = (-2, pre_order)
        phase_number = int(match.group("pre_number"))
    elif dev is not None:
        phase = (-3, 0)
        phase_number = int(dev)
    elif post is not None:
        phase = (1, 0)
        phase_number = int(post)
    else:
        phase = (0, 0)
        phase_number = 0
    dev_number = -1 if dev is None else int(dev)
    return release, phase, phase_number, dev_number


def _compare_versions(left: str, right: str) -> int:
    left_parts = _version_parts(left)
    right_parts = _version_parts(right)
    width = max(len(left_parts[0]), len(right_parts[0]))
    left_release = left_parts[0] + (0,) * (width - len(left_parts[0]))
    right_release = right_parts[0] + (0,) * (width - len(right_parts[0]))
    left_key = (left_release, *left_parts[1:])
    right_key = (right_release, *right_parts[1:])
    return (left_key > right_key) - (left_key < right_key)


def _compatible_release(version: str, requested: str) -> bool:
    if _compare_versions(version, requested) < 0:
        return False
    release = _version_parts(requested)[0]
    prefix = release[:-1] if len(release) > 1 else release
    return _version_parts(version)[0][: len(prefix)] == prefix


def _matches_specifier(version: str, operator: str, requested: str) -> bool:
    if requested.endswith(".*"):
        if operator not in {"==", "!="}:
            raise ValueError(
                "A wildcard core version is valid only with == or !=."
            )
        prefix = _release_parts(requested[:-2])
        matched = _version_parts(version)[0][: len(prefix)] == prefix
        return matched if operator == "==" else not matched
    if operator == "===":
        return version == requested
    comparison = _compare_versions(version, requested)
    if operator == "~=":
        return _compatible_release(version, requested)
    return {
        "==": comparison == 0,
        "!=": comparison != 0,
        "<=": comparison <= 0,
        ">=": comparison >= 0,
        "<": comparison < 0,
        ">": comparison > 0,
    }[operator]


def _core_requirement_matches(version: str, requirement: str) -> bool:
    match = _CORE_REQUIREMENT_RE.fullmatch(requirement)
    if match is None:
        raise ValueError(
            "core_requirements must constrain the canonical "
            "bioimageflow-core distribution."
        )
    specifiers = match.group("specifiers").split(",")
    if not specifiers or any(not specifier for specifier in specifiers):
        raise ValueError("core_requirements contain an empty specifier.")
    matches = []
    for specifier in specifiers:
        specifier_match = _SPECIFIER_RE.fullmatch(specifier)
        if specifier_match is None:
            raise ValueError(
                f"Unsupported bioimageflow-core specifier {specifier!r}."
            )
        matches.append(
            _matches_specifier(
                version,
                specifier_match.group("operator"),
                specifier_match.group("version"),
            )
        )
    return all(matches)


def _decode_payload(
    payload: Mapping[str, Any],
) -> Tuple[
    str,
    Tuple[str, ...],
    str,
    str,
    Tuple[str, ...],
    Tuple[WorkerToolOriginV1, ...],
]:
    data = _exact_dict(
        payload,
        (
            "schema",
            "executor_label",
            "core_requirements",
            "storage_root",
            "sentinel_path",
            "readable_paths",
            "origins",
        ),
        label="executor preflight request",
    )
    if data["schema"] != PREFLIGHT_SCHEMA:
        raise ValueError(
            f"Unsupported executor preflight schema {data['schema']!r}."
        )
    executor_label = _text(data["executor_label"], field="executor_label")
    requirements = _canonical_string_list(
        data["core_requirements"],
        field="core_requirements",
    )
    for requirement in requirements:
        _core_requirement_matches("0.0.0", requirement)
    storage_root = _absolute_path(data["storage_root"], field="storage_root")
    sentinel_path = _absolute_path(
        data["sentinel_path"],
        field="sentinel_path",
    )
    try:
        sentinel_relative = Path(sentinel_path).relative_to(storage_root)
    except ValueError as exc:
        raise ValueError(
            "sentinel_path must be confined beneath storage_root."
        ) from exc
    if (
        not sentinel_relative.parts
        or sentinel_relative.parts[0] in {"cache", "views", "outputs"}
    ):
        raise ValueError(
            "sentinel_path must use a non-cache namespace beneath storage_root."
        )
    readable_paths = _canonical_path_list(data["readable_paths"])
    if storage_root not in readable_paths:
        raise ValueError("readable_paths must include storage_root.")
    return (
        executor_label,
        requirements,
        storage_root,
        sentinel_path,
        readable_paths,
        _origins(data["origins"]),
    )


def _path_result(path_text: str) -> Dict[str, Any]:
    path = Path(path_text)
    resolved = str(path.resolve(strict=False))
    readable = path.exists() and os.access(path, os.R_OK)
    return {
        "path": path_text,
        "resolved_path": resolved,
        "readable": readable,
    }


def _sentinel_result(
    storage_root: str,
    sentinel_path: str,
) -> Tuple[bool, bool, bool]:
    sentinel = Path(sentinel_path)
    storage = Path(storage_root)
    created_parents: List[Path] = []
    current = sentinel.parent
    while current != storage and not current.exists():
        created_parents.append(current)
        current = current.parent

    wrote = False
    read = False
    deleted = False
    created_sentinel = False
    content = b"bioimageflow executor preflight\n"
    try:
        sentinel.parent.mkdir(parents=True, exist_ok=True)
        with sentinel.open("xb") as stream:
            created_sentinel = True
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        wrote = True
        read = sentinel.read_bytes() == content
    except OSError:
        pass
    finally:
        if created_sentinel:
            try:
                sentinel.unlink()
                deleted = not sentinel.exists()
            except OSError:
                deleted = False
        for directory in created_parents:
            try:
                directory.rmdir()
            except OSError:
                pass
    return wrote, read, deleted


def _origin_result(origin: WorkerToolOriginV1) -> Dict[str, Any]:
    identity = worker_tool_origin_identity(origin)
    resolved = False
    try:
        _load_origin_class(origin, identity)
        resolved = True
    except Exception:
        resolved = False
    return {
        "identity": identity,
        "kind": origin.kind,
        "resolved": resolved,
    }


def execute_executor_preflight(
    payload: Mapping[str, Any],
) -> Dict[str, Any]:
    """Verify one executor against a strict plain preflight request."""
    (
        executor_label,
        core_requirements,
        storage_root,
        sentinel_path,
        readable_paths,
        origins,
    ) = _decode_payload(payload)
    try:
        core_version = importlib.metadata.version(_CORE_DISTRIBUTION)
    except importlib.metadata.PackageNotFoundError:
        core_version = "unavailable"
        core_compatible = False
    else:
        try:
            core_compatible = all(
                _core_requirement_matches(core_version, requirement)
                for requirement in core_requirements
            )
        except ValueError:
            core_compatible = False
    sentinel_write, sentinel_read, sentinel_delete = _sentinel_result(
        storage_root,
        sentinel_path,
    )
    return {
        "schema": PREFLIGHT_RESULT_SCHEMA,
        "executor_label": executor_label,
        "worker_api": TASK_SCHEMA,
        "core_version": core_version,
        "core_requirements": list(core_requirements),
        "core_compatible": core_compatible,
        "storage_root": storage_root,
        "sentinel_path": sentinel_path,
        "sentinel_write": sentinel_write,
        "sentinel_read": sentinel_read,
        "sentinel_delete": sentinel_delete,
        "path_results": [_path_result(path) for path in readable_paths],
        "origin_results": [_origin_result(origin) for origin in origins],
    }


__all__ = [
    "PREFLIGHT_RESULT_SCHEMA",
    "PREFLIGHT_SCHEMA",
    "execute_executor_preflight",
]
