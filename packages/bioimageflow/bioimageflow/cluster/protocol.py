"""Strict wire values for the managed-cluster gateway.

The gateway is a one-request process.  This module deliberately contains no SSH or
cluster imports so both the laptop and the installed gateway can validate the same
wire contract before performing an operation.
"""

from __future__ import annotations

import json
import math
import re
import uuid
from dataclasses import dataclass
from typing import Any, ClassVar, Literal, Mapping

from bioimageflow.storage import canonical_json_bytes

from ._common import DIGEST_RE, canonical_digest, freeze_json, thaw_json


PROTOCOL_VERSION = 1
REQUEST_SCHEMA = "bioimageflow.cluster.request.v1"
RESPONSE_SCHEMA = "bioimageflow.cluster.response.v1"
GATEWAY_VERSION = "1.0"
MAX_REQUEST_BYTES = 4 * 1024 * 1024
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_JSON_DEPTH = 64
MAX_JSON_VALUES = 200_000
_OPERATION_RE = re.compile(r"^[a-z][a-z0-9_-]{0,127}$")
_OPERATION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@-]{0,127}$")
_DIAGNOSTIC_FIELDS = {
    "schema",
    "phase",
    "category",
    "message",
    "allocation_state",
    "retry_safety",
    "next_action",
    "identities",
}


class GatewayProtocolError(ValueError):
    """A stable failure raised before untrusted gateway data is used."""

    def __init__(self, category: str, message: str) -> None:
        self.category = category
        super().__init__(message)


def _canonical_uuid4(value: Any, *, field: str) -> str:
    try:
        parsed = uuid.UUID(value, version=4)
    except (AttributeError, TypeError, ValueError) as exc:
        raise GatewayProtocolError(
            "protocol-incompatible", f"{field} must be a canonical UUID4 string."
        ) from exc
    if str(parsed) != value:
        raise GatewayProtocolError(
            "protocol-incompatible", f"{field} must be a canonical UUID4 string."
        )
    return value


def _reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise GatewayProtocolError(
                "protocol-incompatible", "Gateway JSON contains a duplicate key."
            )
        result[key] = value
    return result


def _reject_nonfinite(value: str) -> None:
    raise GatewayProtocolError(
        "protocol-incompatible", "Gateway JSON contains a non-finite number."
    )


def _check_json_tree(value: Any) -> None:
    count = 0

    def visit(item: Any, depth: int) -> None:
        nonlocal count
        if depth > MAX_JSON_DEPTH:
            raise GatewayProtocolError(
                "resource-limit-exceeded", "Gateway JSON exceeds its nesting limit."
            )
        count += 1
        if count > MAX_JSON_VALUES:
            raise GatewayProtocolError(
                "resource-limit-exceeded", "Gateway JSON contains too many values."
            )
        if item is None or type(item) in {bool, int, str}:
            return
        if type(item) is float:
            if not math.isfinite(item):
                raise GatewayProtocolError(
                    "protocol-incompatible", "Non-finite JSON numbers are forbidden."
                )
            return
        if type(item) is list:
            for child in item:
                visit(child, depth + 1)
            return
        if type(item) is dict:
            for key, child in item.items():
                if type(key) is not str:
                    raise GatewayProtocolError(
                        "protocol-incompatible", "JSON keys must be strings."
                    )
                visit(child, depth + 1)
            return
        raise GatewayProtocolError(
            "protocol-incompatible", "The gateway value is not JSON-safe."
        )

    visit(value, 0)


def _decode_json(encoded: bytes, *, limit: int, noun: str) -> Any:
    if len(encoded) > limit:
        raise GatewayProtocolError(
            "resource-limit-exceeded", f"The cluster {noun} exceeds its byte limit."
        )
    try:
        value = json.loads(
            encoded.decode("utf-8"),
            object_pairs_hook=_reject_duplicates,
            parse_constant=_reject_nonfinite,
        )
    except GatewayProtocolError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GatewayProtocolError(
            "protocol-incompatible", f"The cluster {noun} is not canonical JSON."
        ) from exc
    _check_json_tree(value)
    return value


@dataclass(frozen=True, slots=True)
class GatewayRequest:
    """One validated gateway request.

    ``operation_id`` is required for mutations and absent for observations.  The
    operation digest intentionally excludes ``request_id`` so an acknowledgement
    retry may use a fresh transport request while retaining one logical mutation.
    """

    schema: ClassVar[str] = REQUEST_SCHEMA
    operation: str
    arguments: Mapping[str, Any]
    request_id: str
    operation_id: str | None = None
    payload_digest: str | None = None
    protocol_version: int = PROTOCOL_VERSION

    def __post_init__(self) -> None:
        if type(self.protocol_version) is not int or self.protocol_version != PROTOCOL_VERSION:
            raise GatewayProtocolError(
                "protocol-incompatible", "The gateway protocol version is unsupported."
            )
        _canonical_uuid4(self.request_id, field="request_id")
        if self.operation_id is not None and (
            type(self.operation_id) is not str
            or _OPERATION_ID_RE.fullmatch(self.operation_id) is None
        ):
            raise GatewayProtocolError(
                "protocol-incompatible", "operation_id is not a safe stable identifier."
            )
        if (
            type(self.operation) is not str
            or _OPERATION_RE.fullmatch(self.operation) is None
        ):
            raise GatewayProtocolError(
                "protocol-incompatible", "operation must be a non-empty bounded string."
            )
        if not isinstance(self.arguments, Mapping):
            raise GatewayProtocolError(
                "protocol-incompatible", "arguments must be a JSON object."
            )
        if self.payload_digest is not None and (
            type(self.payload_digest) is not str
            or DIGEST_RE.fullmatch(self.payload_digest) is None
        ):
            raise GatewayProtocolError(
                "protocol-incompatible", "payload_digest must be a SHA-256 digest."
            )
        try:
            frozen = freeze_json(
                self.arguments,
                path="arguments",
                reject_sensitive_keys=False,
            )
        except (TypeError, ValueError) as exc:
            raise GatewayProtocolError(
                "protocol-incompatible", "arguments must contain finite JSON values."
            ) from exc
        object.__setattr__(self, "arguments", frozen)

    @property
    def operation_digest(self) -> str:
        return canonical_digest(self.identity_dict())

    def identity_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "protocol_version": self.protocol_version,
            "operation": self.operation,
            "operation_id": self.operation_id,
            "arguments": thaw_json(self.arguments),
            "payload_digest": self.payload_digest,
        }

    def to_dict(self) -> dict[str, Any]:
        value = self.identity_dict()
        value["request_id"] = self.request_id
        return value

    def encode(self) -> bytes:
        encoded = canonical_json_bytes(self.to_dict())
        if len(encoded) > MAX_REQUEST_BYTES:
            raise GatewayProtocolError(
                "resource-limit-exceeded", "The cluster request exceeds its byte limit."
            )
        return encoded

    @classmethod
    def create(
        cls,
        operation: str,
        arguments: Mapping[str, Any],
        *,
        request_id: str | None = None,
        operation_id: str | None = None,
        payload_digest: str | None = None,
    ) -> "GatewayRequest":
        return cls(
            operation=operation,
            arguments=arguments,
            request_id=request_id or str(uuid.uuid4()),
            operation_id=operation_id,
            payload_digest=payload_digest,
        )

    @classmethod
    def from_dict(cls, value: Any) -> "GatewayRequest":
        fields = {
            "schema",
            "protocol_version",
            "request_id",
            "operation",
            "operation_id",
            "arguments",
            "payload_digest",
        }
        if type(value) is not dict or set(value) != fields:
            raise GatewayProtocolError(
                "protocol-incompatible", "The cluster request has missing or unknown fields."
            )
        if value["schema"] != REQUEST_SCHEMA:
            raise GatewayProtocolError(
                "protocol-incompatible", "The cluster request schema is unsupported."
            )
        return cls(**{key: item for key, item in value.items() if key != "schema"})

    @classmethod
    def decode(cls, encoded: bytes) -> "GatewayRequest":
        return cls.from_dict(
            _decode_json(encoded, limit=MAX_REQUEST_BYTES, noun="request")
        )


@dataclass(frozen=True, slots=True)
class GatewayResponse:
    """One validated gateway response with no raw stderr or exception object."""

    schema: ClassVar[str] = RESPONSE_SCHEMA
    request_id: str
    status: Literal["ok", "error"]
    payload: Mapping[str, Any] | None
    diagnostic: Mapping[str, Any] | None
    gateway_version: str = GATEWAY_VERSION
    supported_protocol_versions: tuple[int, ...] = (PROTOCOL_VERSION,)
    protocol_version: int = PROTOCOL_VERSION

    def __post_init__(self) -> None:
        _canonical_uuid4(self.request_id, field="request_id")
        if type(self.protocol_version) is not int:
            raise GatewayProtocolError(
                "protocol-incompatible", "The response protocol version is invalid."
            )
        if type(self.gateway_version) is not str or not self.gateway_version:
            raise GatewayProtocolError(
                "protocol-incompatible", "The response gateway version is invalid."
            )
        versions = tuple(self.supported_protocol_versions)
        if (
            not versions
            or any(type(version) is not int or version <= 0 for version in versions)
            or len(set(versions)) != len(versions)
        ):
            raise GatewayProtocolError(
                "protocol-incompatible", "Supported protocol versions are invalid."
            )
        if self.status == "ok":
            if not isinstance(self.payload, Mapping) or self.diagnostic is not None:
                raise GatewayProtocolError(
                    "protocol-incompatible", "A successful response must contain one payload."
                )
        elif self.status == "error":
            if self.payload is not None or not isinstance(self.diagnostic, Mapping):
                raise GatewayProtocolError(
                    "protocol-incompatible", "An error response must contain one diagnostic."
                )
        else:
            raise GatewayProtocolError(
                "protocol-incompatible", "The gateway response status is invalid."
            )
        if self.diagnostic is not None:
            diagnostic = self.diagnostic
            identities = diagnostic.get("identities")
            if (
                set(diagnostic) != _DIAGNOSTIC_FIELDS
                or diagnostic.get("schema")
                != "bioimageflow.cluster_diagnostic.v1"
                or any(
                    type(diagnostic.get(name)) is not str
                    or not diagnostic.get(name)
                    for name in ("phase", "category", "message", "next_action")
                )
                or diagnostic.get("allocation_state")
                not in {"none", "orchestrator-submitted", "workers-possible", "unknown"}
                or diagnostic.get("retry_safety")
                not in {"safe", "same-attempt-only", "unsafe", "not-applicable"}
                or not isinstance(identities, Mapping)
                or any(
                    type(key) is not str or type(item) is not str
                    for key, item in identities.items()
                )
            ):
                raise GatewayProtocolError(
                    "protocol-incompatible", "The response diagnostic is invalid."
                )
        for name in ("payload", "diagnostic"):
            item = getattr(self, name)
            if item is not None:
                try:
                    item = freeze_json(item, path=name, reject_sensitive_keys=False)
                except (TypeError, ValueError) as exc:
                    raise GatewayProtocolError(
                        "protocol-incompatible", f"The response {name} is not JSON-safe."
                    ) from exc
                object.__setattr__(self, name, item)
        object.__setattr__(self, "supported_protocol_versions", versions)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "protocol_version": self.protocol_version,
            "request_id": self.request_id,
            "status": self.status,
            "payload": None if self.payload is None else thaw_json(self.payload),
            "diagnostic": (
                None if self.diagnostic is None else thaw_json(self.diagnostic)
            ),
            "gateway_version": self.gateway_version,
            "supported_protocol_versions": list(self.supported_protocol_versions),
        }

    def encode(self) -> bytes:
        encoded = canonical_json_bytes(self.to_dict())
        if len(encoded) > MAX_RESPONSE_BYTES:
            raise GatewayProtocolError(
                "resource-limit-exceeded", "The cluster response exceeds its byte limit."
            )
        return encoded

    @classmethod
    def ok(
        cls, request_id: str, payload: Mapping[str, Any]
    ) -> "GatewayResponse":
        return cls(request_id, "ok", payload, None)

    @classmethod
    def error(
        cls, request_id: str, diagnostic: Mapping[str, Any]
    ) -> "GatewayResponse":
        return cls(request_id, "error", None, diagnostic)

    @classmethod
    def from_dict(cls, value: Any) -> "GatewayResponse":
        fields = {
            "schema",
            "protocol_version",
            "request_id",
            "status",
            "payload",
            "diagnostic",
            "gateway_version",
            "supported_protocol_versions",
        }
        if type(value) is not dict or set(value) != fields:
            raise GatewayProtocolError(
                "protocol-incompatible", "The gateway response has missing or unknown fields."
            )
        if value["schema"] != RESPONSE_SCHEMA:
            raise GatewayProtocolError(
                "protocol-incompatible", "The gateway response schema is unsupported."
            )
        versions = value["supported_protocol_versions"]
        if type(versions) is not list:
            raise GatewayProtocolError(
                "protocol-incompatible", "Supported protocol versions must be a list."
            )
        return cls(
            request_id=value["request_id"],
            status=value["status"],
            payload=value["payload"],
            diagnostic=value["diagnostic"],
            gateway_version=value["gateway_version"],
            supported_protocol_versions=tuple(versions),
            protocol_version=value["protocol_version"],
        )

    @classmethod
    def decode(cls, encoded: bytes) -> "GatewayResponse":
        return cls.from_dict(
            _decode_json(encoded, limit=MAX_RESPONSE_BYTES, noun="response")
        )


__all__ = [
    "GATEWAY_VERSION",
    "MAX_REQUEST_BYTES",
    "MAX_RESPONSE_BYTES",
    "PROTOCOL_VERSION",
    "REQUEST_SCHEMA",
    "RESPONSE_SCHEMA",
    "GatewayProtocolError",
    "GatewayRequest",
    "GatewayResponse",
]
