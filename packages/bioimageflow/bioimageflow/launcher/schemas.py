"""Strict wire-schema validation for durable launcher control files."""

from __future__ import annotations

import math
import os
import re
import uuid
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SUBMISSION_SCHEMA = "bioimageflow.launcher.submission.v1"
STATUS_SCHEMA = "bioimageflow.launcher.status.v1"
CLAIM_SCHEMA = "bioimageflow.launcher.claim.v1"
PROGRESS_SCHEMA = "bioimageflow.launcher.progress.v1"
ERROR_SCHEMA = "bioimageflow.launcher.error.v1"
RETURN_SCHEMA = "bioimageflow.launcher.return.v1"

RUN_ID_PATTERN = re.compile(r"^run_[0-9a-f]{32}$")
SHA256_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")

LAUNCHER_STATES = frozenset(
    {
        "prepared",
        "starting",
        "running",
        "finalizing",
        "cancel_requested",
        "succeeded",
        "failed",
        "cancelled",
        "lost",
    }
)
TERMINAL_STATES = frozenset({"succeeded", "failed", "cancelled", "lost"})
PROGRESS_KINDS = frozenset({"public", "backend"})

SUBMISSION_FIELDS = frozenset(
    {
        "schema",
        "run_id",
        "created_at",
        "storage_root",
        "canonical_view",
        "workflow",
        "invocation",
        "parsl_config",
        "executor_bindings",
        "node_routes",
        "environment_routes",
        "shared_runtime_root",
        "task_policy",
        "launch",
        "protocol_versions",
    }
)
STATUS_FIELDS = frozenset(
    {
        "schema",
        "run_id",
        "state",
        "revision",
        "created_at",
        "updated_at",
        "backend",
        "orchestrator",
        "claim_epoch",
        "cancel_requested_at",
        "hard_termination_requested",
        "error",
    }
)
CLAIM_FIELDS = frozenset(
    {
        "schema",
        "run_id",
        "owner",
        "backend",
        "nonce",
        "epoch",
        "created_at",
        "heartbeat_at",
        "expires_at",
    }
)
PROGRESS_FIELDS = frozenset(
    {"schema", "run_id", "sequence", "timestamp", "kind", "payload"}
)
ERROR_FIELDS = frozenset(
    {
        "schema",
        "run_id",
        "code",
        "exception_type",
        "message",
        "traceback",
        "node",
        "task",
        "backend",
    }
)
RETURN_FIELDS = frozenset(
    {
        "schema",
        "run_id",
        "shape",
        "mapping_keys",
        "frames",
        "root_outputs",
        "locators",
    }
)


class LauncherSchemaError(ValueError):
    """Raised when a launcher control payload violates its wire schema."""


def validate_run_id(value: object) -> str:
    """Return a valid UUID4 launcher run ID."""
    if not isinstance(value, str) or RUN_ID_PATTERN.fullmatch(value) is None:
        raise LauncherSchemaError(
            "run_id must use 'run_' followed by 32 lowercase hexadecimal characters."
        )
    try:
        parsed = uuid.UUID(hex=value[4:])
    except ValueError as error:
        raise LauncherSchemaError("run_id contains an invalid UUID.") from error
    if parsed.version != 4 or parsed.variant != uuid.RFC_4122:
        raise LauncherSchemaError("run_id must contain an RFC 4122 UUID4 value.")
    return value


def new_run_id() -> str:
    """Return a fresh path-safe UUID4 run ID."""
    return f"run_{uuid.uuid4().hex}"


def utc_timestamp(value: datetime | None = None) -> str:
    """Return a normalized UTC timestamp for launcher metadata."""
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None or current.utcoffset() is None:
        raise LauncherSchemaError("Launcher timestamps must be timezone-aware.")
    return current.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_utc_timestamp(value: object, *, field: str) -> datetime:
    """Parse a launcher UTC timestamp and reject non-UTC or naive values."""
    if not isinstance(value, str) or not value:
        raise LauncherSchemaError(f"{field} must be a non-empty UTC timestamp.")
    encoded = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(encoded)
    except ValueError as error:
        raise LauncherSchemaError(f"{field} is not a valid timestamp.") from error
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise LauncherSchemaError(f"{field} must be expressed in UTC.")
    return parsed


def _mapping(
    payload: object,
    *,
    schema: str,
    fields: frozenset[str],
) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        raise LauncherSchemaError(f"{schema} must be a JSON object.")
    if not all(isinstance(key, str) for key in payload):
        raise LauncherSchemaError(f"{schema} object keys must be strings.")
    result = dict(payload)
    actual = frozenset(result)
    if actual != fields:
        missing = sorted(fields - actual)
        unknown = sorted(actual - fields)
        details: list[str] = []
        if missing:
            details.append(f"missing fields {missing}")
        if unknown:
            details.append(f"unknown fields {unknown}")
        raise LauncherSchemaError(f"{schema} has {' and '.join(details)}.")
    if result["schema"] != schema:
        raise LauncherSchemaError(
            f"Expected schema {schema!r}, got {result['schema']!r}."
        )
    _validate_json_value(result, field=schema)
    return result


def _validate_json_value(value: object, *, field: str) -> None:
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise LauncherSchemaError(f"{field} contains a non-finite number.")
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise LauncherSchemaError(f"{field} contains a non-string object key.")
            _validate_json_value(item, field=f"{field}.{key}")
        return
    if isinstance(value, Sequence) and not isinstance(
        value,
        (str, bytes, bytearray),
    ):
        for index, item in enumerate(value):
            _validate_json_value(item, field=f"{field}[{index}]")
        return
    raise LauncherSchemaError(
        f"{field} contains non-JSON value {type(value).__name__}."
    )


def _require_string(value: object, *, field: str, nullable: bool = False) -> None:
    if nullable and value is None:
        return
    if not isinstance(value, str) or not value:
        suffix = " or null" if nullable else ""
        raise LauncherSchemaError(f"{field} must be a non-empty string{suffix}.")


def _require_mapping(
    value: object,
    *,
    field: str,
    nullable: bool = False,
) -> None:
    if nullable and value is None:
        return
    if not isinstance(value, Mapping):
        suffix = " or null" if nullable else ""
        raise LauncherSchemaError(f"{field} must be an object{suffix}.")


def _require_string_mapping(
    value: object,
    *,
    field: str,
    nullable: bool = False,
) -> None:
    if nullable and value is None:
        return
    _require_mapping(value, field=field)
    assert isinstance(value, Mapping)
    if not all(isinstance(key, str) and isinstance(item, str) for key, item in value.items()):
        raise LauncherSchemaError(f"{field} must map strings to strings.")


def _validate_absolute_path(value: object, *, field: str, nullable: bool = False) -> None:
    if nullable and value is None:
        return
    _require_string(value, field=field)
    assert isinstance(value, str)
    path = Path(value)
    if not path.is_absolute() or os.path.normpath(value) != value:
        raise LauncherSchemaError(f"{field} must be a normalized absolute path.")


def _validate_relative_posix_path(value: object, *, field: str) -> str:
    _require_string(value, field=field)
    assert isinstance(value, str)
    if "\x00" in value or value.startswith("/") or "\\" in value:
        raise LauncherSchemaError(f"{field} must be a confined relative POSIX path.")
    if any(part in {"", ".", ".."} for part in value.split("/")):
        raise LauncherSchemaError(f"{field} must be a confined relative POSIX path.")
    return value


def validate_submission(payload: object) -> dict[str, Any]:
    """Validate and copy a submission-v1 payload."""
    result = _mapping(
        payload,
        schema=SUBMISSION_SCHEMA,
        fields=SUBMISSION_FIELDS,
    )
    run_id = validate_run_id(result["run_id"])
    parse_utc_timestamp(result["created_at"], field="created_at")
    _validate_absolute_path(result["storage_root"], field="storage_root")
    canonical_view = _validate_relative_posix_path(
        result["canonical_view"],
        field="canonical_view",
    )
    if canonical_view != f"views/runs/{run_id}":
        raise LauncherSchemaError(
            "canonical_view must be the run's exact canonical view path."
        )
    workflow = result["workflow"]
    _require_mapping(workflow, field="workflow")
    assert isinstance(workflow, Mapping)
    if frozenset(workflow) != {"kind", "digest", "payload"}:
        raise LauncherSchemaError(
            "workflow must contain exactly kind, digest, and payload."
        )
    _require_string(workflow["kind"], field="workflow.kind")
    if (
        not isinstance(workflow["digest"], str)
        or SHA256_PATTERN.fullmatch(workflow["digest"]) is None
    ):
        raise LauncherSchemaError(
            "workflow.digest must use 'sha256:' and a lowercase SHA-256 digest."
        )
    _require_mapping(workflow["payload"], field="workflow.payload")
    _require_mapping(result["invocation"], field="invocation")
    _require_mapping(result["parsl_config"], field="parsl_config")
    _require_mapping(result["executor_bindings"], field="executor_bindings")
    _require_string_mapping(
        result["node_routes"],
        field="node_routes",
        nullable=True,
    )
    _require_string_mapping(
        result["environment_routes"],
        field="environment_routes",
        nullable=True,
    )
    _validate_absolute_path(
        result["shared_runtime_root"],
        field="shared_runtime_root",
        nullable=True,
    )
    _require_mapping(result["task_policy"], field="task_policy")
    _require_mapping(result["launch"], field="launch")
    _require_mapping(result["protocol_versions"], field="protocol_versions")
    return result


def validate_status(payload: object) -> dict[str, Any]:
    """Validate and copy a status-v1 payload."""
    result = _mapping(payload, schema=STATUS_SCHEMA, fields=STATUS_FIELDS)
    validate_run_id(result["run_id"])
    if result["state"] not in LAUNCHER_STATES:
        raise LauncherSchemaError(f"Invalid launcher state {result['state']!r}.")
    if (
        not isinstance(result["revision"], int)
        or isinstance(result["revision"], bool)
        or result["revision"] < 0
    ):
        raise LauncherSchemaError("revision must be a non-negative integer.")
    created = parse_utc_timestamp(result["created_at"], field="created_at")
    updated = parse_utc_timestamp(result["updated_at"], field="updated_at")
    if updated < created:
        raise LauncherSchemaError("updated_at must not precede created_at.")
    _require_string(result["backend"], field="backend")
    if result["orchestrator"] is not None:
        _validate_json_value(result["orchestrator"], field="orchestrator")
    epoch = result["claim_epoch"]
    if epoch is not None and (
        not isinstance(epoch, int) or isinstance(epoch, bool) or epoch < 1
    ):
        raise LauncherSchemaError("claim_epoch must be a positive integer or null.")
    if result["cancel_requested_at"] is not None:
        parse_utc_timestamp(
            result["cancel_requested_at"],
            field="cancel_requested_at",
        )
    if not isinstance(result["hard_termination_requested"], bool):
        raise LauncherSchemaError("hard_termination_requested must be a bool.")
    return result


def validate_claim(payload: object) -> dict[str, Any]:
    """Validate and copy a claim-v1 payload."""
    result = _mapping(payload, schema=CLAIM_SCHEMA, fields=CLAIM_FIELDS)
    validate_run_id(result["run_id"])
    _require_string(result["owner"], field="owner")
    _require_string(result["backend"], field="backend")
    _require_string(result["nonce"], field="nonce")
    epoch = result["epoch"]
    if not isinstance(epoch, int) or isinstance(epoch, bool) or epoch < 1:
        raise LauncherSchemaError("epoch must be a positive integer.")
    created = parse_utc_timestamp(result["created_at"], field="created_at")
    heartbeat = parse_utc_timestamp(result["heartbeat_at"], field="heartbeat_at")
    expires = parse_utc_timestamp(result["expires_at"], field="expires_at")
    if heartbeat < created:
        raise LauncherSchemaError("heartbeat_at must not precede created_at.")
    if expires <= heartbeat:
        raise LauncherSchemaError("expires_at must follow heartbeat_at.")
    return result


def validate_progress(payload: object) -> dict[str, Any]:
    """Validate and copy a progress-v1 payload."""
    result = _mapping(payload, schema=PROGRESS_SCHEMA, fields=PROGRESS_FIELDS)
    validate_run_id(result["run_id"])
    sequence = result["sequence"]
    if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 0:
        raise LauncherSchemaError("sequence must be a non-negative integer.")
    parse_utc_timestamp(result["timestamp"], field="timestamp")
    if result["kind"] not in PROGRESS_KINDS:
        raise LauncherSchemaError("kind must be 'public' or 'backend'.")
    _require_mapping(result["payload"], field="payload")
    return result


def validate_error(payload: object) -> dict[str, Any]:
    """Validate and copy an error-v1 payload."""
    result = _mapping(payload, schema=ERROR_SCHEMA, fields=ERROR_FIELDS)
    validate_run_id(result["run_id"])
    _require_string(result["code"], field="code")
    _require_string(result["exception_type"], field="exception_type", nullable=True)
    for field in ("message", "traceback"):
        if result[field] is not None and not isinstance(result[field], str):
            raise LauncherSchemaError(f"{field} must be a string or null.")
    return result


def validate_return(payload: object) -> dict[str, Any]:
    """Validate and copy a return-v1 payload."""
    result = _mapping(payload, schema=RETURN_SCHEMA, fields=RETURN_FIELDS)
    validate_run_id(result["run_id"])
    if result["shape"] not in {"single", "mapping"}:
        raise LauncherSchemaError("shape must be 'single' or 'mapping'.")
    if not isinstance(result["mapping_keys"], list) or not all(
        isinstance(item, str) for item in result["mapping_keys"]
    ):
        raise LauncherSchemaError("mapping_keys must be an ordered string array.")
    if len(set(result["mapping_keys"])) != len(result["mapping_keys"]):
        raise LauncherSchemaError("mapping_keys must be unique.")
    if result["shape"] == "single" and result["mapping_keys"]:
        raise LauncherSchemaError("single returns must not contain mapping_keys.")
    for field in ("frames", "root_outputs", "locators"):
        if not isinstance(result[field], list):
            raise LauncherSchemaError(f"{field} must be an array.")
    return result


SCHEMA_VALIDATORS = {
    SUBMISSION_SCHEMA: validate_submission,
    STATUS_SCHEMA: validate_status,
    CLAIM_SCHEMA: validate_claim,
    PROGRESS_SCHEMA: validate_progress,
    ERROR_SCHEMA: validate_error,
    RETURN_SCHEMA: validate_return,
}


def validate_versioned_payload(payload: object) -> dict[str, Any]:
    """Validate one of the six exact launcher-v1 payloads."""
    if not isinstance(payload, Mapping):
        raise LauncherSchemaError("Launcher payload must be a JSON object.")
    schema = payload.get("schema")
    if not isinstance(schema, str) or schema not in SCHEMA_VALIDATORS:
        raise LauncherSchemaError(f"Unknown launcher schema {schema!r}.")
    return SCHEMA_VALIDATORS[schema](payload)
