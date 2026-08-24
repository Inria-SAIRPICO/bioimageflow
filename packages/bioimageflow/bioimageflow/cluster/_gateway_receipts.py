"""Durable idempotency-receipt operations for the cluster gateway."""
# pyright: reportAttributeAccessIssue=false

from __future__ import annotations

import json
import os
import stat
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from bioimageflow.storage import canonical_json_bytes

from ._common import DIGEST_RE, canonical_digest
from ._gateway_support import (
    RECEIPT_SCHEMA,
    _OPERATION_ID_RE,
    GatewayOperationFailure,
    _atomic_private_json,
    _failure,
)
from .protocol import GatewayRequest


class GatewayReceiptMixin:
    def _receipt_path(self, operation_id: str) -> Path:
        if (
            type(operation_id) is not str
            or _OPERATION_ID_RE.fullmatch(operation_id) is None
        ):
            raise _failure(
                "protocol-incompatible", "operation_id is not a safe stable identifier."
            )
        return self.root / "operations" / f"{operation_id}.json"

    def _read_receipt(self, operation_id: str) -> dict[str, Any] | None:
        path = self._receipt_path(operation_id)
        try:
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        except FileNotFoundError:
            return None
        try:
            before = os.fstat(descriptor)
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_uid != os.geteuid()
                or stat.S_IMODE(before.st_mode) & 0o077
                or before.st_nlink != 1
            ):
                raise _failure(
                    "operation-record-tampered", "The operation receipt is unsafe."
                )
            chunks: list[bytes] = []
            size = 0
            while chunk := os.read(descriptor, 64 * 1024):
                size += len(chunk)
                if size > 1024 * 1024:
                    raise _failure(
                        "operation-record-tampered", "The operation receipt is oversized."
                    )
                chunks.append(chunk)
            after = os.fstat(descriptor)
        finally:
            os.close(descriptor)
        if (before.st_dev, before.st_ino, before.st_size) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
        ):
            raise _failure(
                "operation-record-tampered", "The operation receipt changed while read."
            )
        try:
            value = json.loads(b"".join(chunks))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise _failure(
                "operation-record-tampered", "The operation receipt is malformed."
            ) from exc
        fields = {
            "schema",
            "operation_id",
            "operation",
            "request_digest",
            "phase",
            "identities",
            "result",
            "diagnostic",
            "result_digest",
            "diagnostic_digest",
            "revision",
        }
        if (
            type(value) is not dict
            or set(value) != fields
            or value["schema"] != RECEIPT_SCHEMA
            or value["operation_id"] != operation_id
            or type(value["operation"]) is not str
            or type(value["request_digest"]) is not str
            or DIGEST_RE.fullmatch(value["request_digest"]) is None
            or value["phase"] not in {"intent", "completed", "failed"}
            or type(value["identities"]) is not dict
            or type(value["revision"]) is not int
            or value["revision"] < 0
        ):
            raise _failure(
                "operation-record-tampered", "The operation receipt is malformed."
            )
        if value["phase"] == "completed":
            if (
                type(value["result"]) is not dict
                or value["diagnostic"] is not None
                or value["result_digest"] != canonical_digest(value["result"])
                or value["diagnostic_digest"] is not None
            ):
                raise _failure(
                    "operation-record-tampered", "The operation receipt digest is invalid."
                )
        elif value["phase"] == "failed":
            if (
                type(value["diagnostic"]) is not dict
                or value["result"] is not None
                or value["diagnostic_digest"]
                != canonical_digest(value["diagnostic"])
                or value["result_digest"] is not None
            ):
                raise _failure(
                    "operation-record-tampered", "The operation receipt digest is invalid."
                )
        elif any(
            value[name] is not None
            for name in ("result", "diagnostic", "result_digest", "diagnostic_digest")
        ):
            raise _failure(
                "operation-record-tampered", "The operation intent is malformed."
            )
        return value

    def _write_receipt(self, operation_id: str, value: Mapping[str, Any]) -> None:
        path = self._receipt_path(operation_id)
        normalized = json.loads(canonical_json_bytes(value))
        _atomic_private_json(path, normalized)
        observed = self._read_receipt(operation_id)
        if observed != normalized:
            raise _failure(
                "operation-record-tampered", "The operation receipt was not durable."
            )

    def mutate(
        self,
        request: GatewayRequest,
        handler: Callable[[Mapping[str, Any]], Mapping[str, Any]],
    ) -> dict[str, Any]:
        if request.operation_id is None:
            raise _failure(
                "protocol-incompatible", "A mutating operation requires operation_id."
            )
        payload_identity_field = {


            "commit_upload": "digest",
            "publish_deployment": "object_id",
        }.get(request.operation)
        if payload_identity_field is not None and (
            request.payload_digest is None
            or request.arguments.get(payload_identity_field) != request.payload_digest
        ):
            raise _failure(
                "protocol-incompatible",
                "The request payload digest does not match its referenced bytes.",
            )
        existing = self._read_receipt(request.operation_id)
        if existing is not None:
            if (
                existing["operation"] != request.operation
                or existing["request_digest"] != request.operation_digest
            ):
                raise _failure(
                    "operation-conflict",
                    "The operation ID is already bound to different request bytes.",
                    retry_safety="not-applicable",
                    next_action="create-new-operation",
                )
            if existing["phase"] == "completed":
                return dict(existing["result"])
            if existing["phase"] == "failed":
                diagnostic = existing["diagnostic"]
                assert type(diagnostic) is dict
                raise GatewayOperationFailure(
                    diagnostic["category"],
                    diagnostic["message"],
                    phase=diagnostic["phase"],
                    allocation_state=diagnostic["allocation_state"],
                    retry_safety=diagnostic["retry_safety"],
                    next_action=diagnostic["next_action"],
                    identities=diagnostic["identities"],
                )
            if request.operation != "apply-cleanup":
                raise _failure(
                    "submission-uncertain",
                    "The operation intent is durable but completion is unknown.",
                    allocation_state="unknown",
                    retry_safety="same-attempt-only",
                    next_action="attach-run-or-retry-same-attempt",
                )
            receipt = dict(existing)
        else:
            receipt = {
                "schema": RECEIPT_SCHEMA,
                "operation_id": request.operation_id,
                "operation": request.operation,
                "request_digest": request.operation_digest,
                "phase": "intent",
                "identities": {},
                "result": None,
                "diagnostic": None,
                "result_digest": None,
                "diagnostic_digest": None,
                "revision": 0,
            }
            self._write_receipt(request.operation_id, receipt)
        try:
            raw = handler(request.arguments)
            if not isinstance(raw, Mapping):
                raise TypeError("A gateway handler must return a mapping.")
            result = dict(raw)
        except GatewayOperationFailure as exc:
            receipt.update(
                phase="failed",
                diagnostic=exc.diagnostic,
                diagnostic_digest=canonical_digest(exc.diagnostic),
                revision=1,
            )
            self._write_receipt(request.operation_id, receipt)
            raise
        except Exception as exc:
            failure = _failure(
                "remote-operation-failed",
                "The gateway operation failed without exposing internal details.",
                retry_safety="same-attempt-only",
                next_action="inspect-private-cluster-log",
            )
            receipt.update(
                phase="failed",
                diagnostic=failure.diagnostic,
                diagnostic_digest=canonical_digest(failure.diagnostic),
                revision=1,
            )
            self._write_receipt(request.operation_id, receipt)
            raise failure from exc
        receipt.update(
            phase="completed",
            result=result,
            result_digest=canonical_digest(result),
            revision=1,
        )
        self._write_receipt(request.operation_id, receipt)
        return result




__all__ = ["GatewayReceiptMixin"]
