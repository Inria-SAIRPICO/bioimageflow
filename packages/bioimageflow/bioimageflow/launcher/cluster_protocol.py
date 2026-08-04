"""Bounded one-shot protocol for the cluster submission agent."""

from __future__ import annotations

import json
import math
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from bioimageflow.storage import canonical_json_bytes


REQUEST_SCHEMA = "bioimageflow.cluster.command.v1"
RESPONSE_SCHEMA = "bioimageflow.cluster.response.v1"
MAX_REQUEST_BYTES = 4 * 1024 * 1024
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_JSON_DEPTH = 64
MAX_JSON_VALUES = 200_000
OPERATIONS = frozenset(
    {
        "allocate-upload",
        "cancel",
        "commit-upload",
        "inspect",
        "prepare-result",
        "plan-retry",
        "read-logs",
        "read-progress",
        "refresh",
        "start-retry",
        "submit",
        "validate-profile",
    }
)


@dataclass(frozen=True, slots=True)
class ClusterProtocolFailure(Exception):
    """Stable protocol error safe to return to an untrusted client."""

    code: str
    message: str
    retryable: bool = False

    def __str__(self) -> str:
        return self.message


def canonical_digest(value: Mapping[str, Any]) -> str:
    """Return the canonical SHA-256 identity of one JSON object."""
    import hashlib

    return f"sha256:{hashlib.sha256(canonical_json_bytes(value)).hexdigest()}"


def _reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ClusterProtocolFailure(
                "duplicate-json-key",
                f"Duplicate JSON key {key!r}.",
            )
        result[key] = value
    return result


def _reject_nonfinite(value: str) -> None:
    raise ClusterProtocolFailure(
        "nonfinite-json",
        f"Non-finite JSON value {value!r} is forbidden.",
    )


def _check_tree(value: Any) -> None:
    count = 0

    def visit(item: Any, depth: int) -> None:
        nonlocal count
        if depth > MAX_JSON_DEPTH:
            raise ClusterProtocolFailure(
                "request-too-deep",
                "JSON nesting exceeds the protocol limit.",
            )
        count += 1
        if count > MAX_JSON_VALUES:
            raise ClusterProtocolFailure(
                "request-too-large",
                "JSON contains too many values.",
            )
        if item is None or type(item) in {bool, int, str}:
            return
        if type(item) is float:
            if not math.isfinite(item):
                raise ClusterProtocolFailure(
                    "nonfinite-json",
                    "Non-finite JSON numbers are forbidden.",
                )
            return
        if type(item) is list:
            for child in item:
                visit(child, depth + 1)
            return
        if type(item) is dict:
            for key, child in item.items():
                if type(key) is not str:
                    raise ClusterProtocolFailure(
                        "invalid-json",
                        "JSON object keys must be strings.",
                    )
                visit(child, depth + 1)
            return
        raise ClusterProtocolFailure(
            "invalid-json",
            f"Unsupported JSON value {type(item).__name__}.",
        )

    visit(value, 0)


def decode_request(encoded: bytes) -> dict[str, Any]:
    """Decode and validate one exact cluster-agent request envelope."""
    if len(encoded) > MAX_REQUEST_BYTES:
        raise ClusterProtocolFailure(
            "request-too-large",
            "Cluster request exceeds the byte limit.",
        )
    try:
        value = json.loads(
            encoded.decode("utf-8"),
            object_pairs_hook=_reject_duplicates,
            parse_constant=_reject_nonfinite,
        )
    except ClusterProtocolFailure:
        raise
    except UnicodeDecodeError as exc:
        raise ClusterProtocolFailure(
            "invalid-utf8",
            "Cluster request is not valid UTF-8.",
        ) from exc
    except json.JSONDecodeError as exc:
        raise ClusterProtocolFailure(
            "invalid-json",
            "Cluster request is not valid JSON.",
        ) from exc
    _check_tree(value)
    if type(value) is not dict or set(value) != {
        "arguments",
        "operation",
        "request_id",
        "schema",
    }:
        raise ClusterProtocolFailure(
            "invalid-request",
            "Cluster request envelope contains missing or unknown fields.",
        )
    if value["schema"] != REQUEST_SCHEMA:
        raise ClusterProtocolFailure(
            "unsupported-protocol",
            "Cluster request schema is unsupported.",
        )
    try:
        parsed_id = uuid.UUID(value["request_id"], version=4)
    except (AttributeError, TypeError, ValueError) as exc:
        raise ClusterProtocolFailure(
            "invalid-request-id",
            "request_id must be a canonical UUID4 string.",
        ) from exc
    if str(parsed_id) != value["request_id"]:
        raise ClusterProtocolFailure(
            "invalid-request-id",
            "request_id must be a canonical UUID4 string.",
        )
    if value["operation"] not in OPERATIONS:
        raise ClusterProtocolFailure(
            "unsupported-operation",
            "Cluster operation is unsupported.",
        )
    if type(value["arguments"]) is not dict:
        raise ClusterProtocolFailure(
            "invalid-arguments",
            "Cluster operation arguments must be an object.",
        )
    return value


def request(
    operation: str,
    arguments: Mapping[str, Any],
    *,
    request_id: str | None = None,
) -> dict[str, Any]:
    """Build one strict request envelope."""
    value = {
        "arguments": dict(arguments),
        "operation": operation,
        "request_id": request_id or str(uuid.uuid4()),
        "schema": REQUEST_SCHEMA,
    }
    return decode_request(canonical_json_bytes(value))


def success_response(request_id: str, result: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "error": None,
        "ok": True,
        "request_id": request_id,
        "result": dict(result),
        "schema": RESPONSE_SCHEMA,
    }


def error_response(
    request_id: str | None,
    failure: ClusterProtocolFailure,
) -> dict[str, Any]:
    return {
        "error": {
            "code": failure.code,
            "message": failure.message,
            "retryable": failure.retryable,
        },
        "ok": False,
        "request_id": request_id,
        "result": None,
        "schema": RESPONSE_SCHEMA,
    }


def encode_response(response: Mapping[str, Any]) -> bytes:
    encoded = canonical_json_bytes(response)
    if len(encoded) > MAX_RESPONSE_BYTES:
        raise ClusterProtocolFailure(
            "response-too-large",
            "Cluster response exceeds the byte limit.",
        )
    return encoded


def dispatch(
    encoded: bytes,
    handler: Callable[[str, str, dict[str, Any], str], Mapping[str, Any]],
) -> dict[str, Any]:
    """Decode one request and call a bounded operation handler."""
    value = decode_request(encoded)
    digest = canonical_digest(
        {
            "arguments": value["arguments"],
            "operation": value["operation"],
            "schema": value["schema"],
        }
    )
    result = handler(
        value["operation"],
        value["request_id"],
        value["arguments"],
        digest,
    )
    return success_response(value["request_id"], result)
