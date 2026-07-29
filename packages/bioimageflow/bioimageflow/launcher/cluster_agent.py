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
from .remote_control import (
    cancel_run,
    inspect_run,
    read_log_page,
    read_progress_page,
    refresh_run,
)
from .result_bundle import prepare_result


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
    if operation == "inspect":
        value = _exact_arguments(arguments, {"run_id", "storage_path"})
        return inspect_run(value["storage_path"], value["run_id"])
    if operation == "refresh":
        value = _exact_arguments(arguments, {"run_id", "storage_path"})
        return refresh_run(value["storage_path"], value["run_id"])
    if operation == "read-progress":
        value = _exact_arguments(
            arguments,
            {"after_sequence", "limit", "run_id", "storage_path"},
        )
        return read_progress_page(
            value["storage_path"],
            value["run_id"],
            value["after_sequence"],
            value["limit"],
        )
    if operation == "read-logs":
        value = _exact_arguments(
            arguments,
            {
                "identity",
                "limit",
                "offset",
                "run_id",
                "snapshot_size",
                "storage_path",
                "stream",
            },
        )
        return read_log_page(
            value["storage_path"],
            value["run_id"],
            value["stream"],
            value["offset"],
            value["identity"],
            value["snapshot_size"],
            value["limit"],
        )
    if operation == "cancel":
        value = _exact_arguments(
            arguments,
            {"run_id", "staging_root", "storage_path"},
        )
        return cancel_run(
            value["staging_root"],
            value["storage_path"],
            value["run_id"],
            request_id,
            request_digest,
        )
    if operation == "prepare-result":
        value = _exact_arguments(
            arguments,
            {"run_id", "staging_root", "storage_path"},
        )
        return prepare_result(
            value["staging_root"],
            value["storage_path"],
            value["run_id"],
            request_id,
            request_digest,
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
