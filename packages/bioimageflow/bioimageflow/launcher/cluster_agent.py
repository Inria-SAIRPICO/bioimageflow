"""Installed one-shot command for laptop-to-cluster submission."""

from __future__ import annotations

import sys
from collections.abc import Mapping
from contextlib import redirect_stdout
from typing import Any

from .cluster_protocol import (
    MAX_REQUEST_BYTES,
    ClusterProtocolFailure,
    canonical_digest,
    decode_request,
    encode_response,
    error_response,
    success_response,
)
from .cluster_submit import submit_bundle
from .cluster_upload import allocate_upload, commit_upload, normalized_root


def _exact_arguments(
    value: dict[str, Any],
    expected: set[str],
) -> dict[str, Any]:
    if set(value) != expected:
        raise ClusterProtocolFailure(
            "invalid-arguments",
            "Operation arguments contain missing or unknown fields.",
        )
    return value


def handle_operation(
    operation: str,
    request_id: str,
    arguments: dict[str, Any],
    request_digest: str,
) -> Mapping[str, Any]:
    """Dispatch one already decoded mutable operation."""
    if operation == "allocate-upload":
        value = _exact_arguments(arguments, {"manifest", "staging_root"})
        return allocate_upload(
            normalized_root(value["staging_root"]),
            request_id,
            request_digest,
            value["manifest"],
        )
    if operation == "commit-upload":
        value = _exact_arguments(
            arguments,
            {"manifest", "staging_root", "upload_id"},
        )
        return commit_upload(
            normalized_root(value["staging_root"]),
            request_id,
            request_digest,
            value["upload_id"],
            value["manifest"],
        )
    if operation == "submit":
        value = _exact_arguments(
            arguments,
            {"manifest", "object_path", "staging_root"},
        )
        return submit_bundle(
            normalized_root(value["staging_root"]),
            request_id,
            request_digest,
            value["object_path"],
            value["manifest"],
        )
    raise ClusterProtocolFailure(
        "unsupported-operation",
        "Cluster operation is unsupported.",
    )


def run_agent(encoded: bytes) -> bytes:
    """Execute one protocol request without writing diagnostics."""
    request_id: str | None = None
    try:
        value = decode_request(encoded)
        decoded_request_id = value["request_id"]
        assert type(decoded_request_id) is str
        request_id = decoded_request_id
        digest = canonical_digest(
            {
                "arguments": value["arguments"],
                "operation": value["operation"],
                "schema": value["schema"],
            }
        )
        with redirect_stdout(sys.stderr):
            result = handle_operation(
                value["operation"],
                request_id,
                value["arguments"],
                digest,
            )
        response = success_response(request_id, result)
    except ClusterProtocolFailure as failure:
        response = error_response(request_id, failure)
    except Exception:
        response = error_response(
            request_id,
            ClusterProtocolFailure(
                "internal-error",
                "Cluster agent failed without exposing internal details.",
            ),
        )
    return encode_response(response)


def main() -> int:
    """Read one bounded request from stdin and emit protocol JSON only."""
    encoded = sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1)
    try:
        response = run_agent(encoded)
    except ClusterProtocolFailure as failure:
        response = encode_response(error_response(None, failure))
    sys.stdout.buffer.write(response)
    sys.stdout.buffer.write(b"\n")
    sys.stdout.buffer.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
