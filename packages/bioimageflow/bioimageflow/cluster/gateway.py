"""Installed one-shot managed-cluster gateway and durable receipt store."""

from __future__ import annotations

import os
import sys
from collections.abc import Callable, Mapping
from typing import Any

from ._gateway_archive import (
    _MAX_INVENTORY_ENTRIES,
    _MAX_LOGICAL_PATH_DEPTH,
    _MAX_LOGICAL_PATH_LENGTH,
)
from ._gateway_state import GatewayState
from ._gateway_scripts import (  # noqa: F401 - retained private compatibility seams
    _ATTESTATION_SCRIPT,
    _FACTORY_VALIDATOR_SCRIPT,
    _RUN_CONTROLLER_SCRIPT,
    _RUN_SUBMITTER_SCRIPT,
)
from ._gateway_support import (
    RECEIPT_SCHEMA,
    ROOT_NAMESPACES,
    _MAX_ARCHIVE_ENTRIES,
    _MAX_ARCHIVE_EXPANDED_BYTES,
    _MAX_CHILD_RESPONSE_BYTES,
    _MAX_UPLOAD_BYTES,
    GatewayOperationFailure,
    _failure,
)
from .protocol import (
    GATEWAY_VERSION,
    MAX_REQUEST_BYTES,
    PROTOCOL_VERSION,
    GatewayProtocolError,
    GatewayRequest,
    GatewayResponse,
)


def capabilities() -> dict[str, Any]:
    return {
        "gateway_version": GATEWAY_VERSION,
        "gateway_artifact_digest": os.environ.get(
            "BIOIMAGEFLOW_GATEWAY_ARTIFACT_DIGEST"
        ),
        "gateway_publication_id": os.environ.get(
            "BIOIMAGEFLOW_GATEWAY_PUBLICATION_ID"
        ),
        "supported_protocol_versions": [PROTOCOL_VERSION],
        "request_schema": "bioimageflow.cluster.request.v1",
        "response_schema": "bioimageflow.cluster.response.v1",
        "operation_receipt_schema": RECEIPT_SCHEMA,
        "root_namespaces": list(ROOT_NAMESPACES),
        "operations": [
            "allocate_upload",
            "capabilities",
            "commit_upload",
            "cancel-run",
            "inspect-run",
            "plan-cleanup",
            "plan-retry",
            "prepare-result",
            "apply-cleanup",
            "publish_deployment",
            "read-progress",
            "refresh-run",
            "start-retry",
            "submit-plan",
            "validate-deployment",
        ],
        "limits": {
            "max_request_bytes": MAX_REQUEST_BYTES,
            "max_response_bytes": MAX_REQUEST_BYTES,
            "max_upload_bytes": _MAX_UPLOAD_BYTES,
            "max_archive_entries": _MAX_ARCHIVE_ENTRIES,
            "max_archive_expanded_bytes": _MAX_ARCHIVE_EXPANDED_BYTES,
            "max_child_response_bytes": _MAX_CHILD_RESPONSE_BYTES,
            "max_progress_page": 500,
            "max_inventory_entries": _MAX_INVENTORY_ENTRIES,
            "max_logical_path_length": _MAX_LOGICAL_PATH_LENGTH,
            "max_logical_path_depth": _MAX_LOGICAL_PATH_DEPTH,
            "max_secret_value_bytes": 64 * 1024,
            "max_secret_total_bytes": 256 * 1024,
        },
        "environment_installation_supported": True,
        "existing_python_attestation_supported": True,
        "environment_adapter_versions": {"existing_python": 1, "uv": 1},
    }


def _default_handlers(
    state: GatewayState,
) -> dict[str, Callable[[Mapping[str, Any]], Mapping[str, Any]]]:
    return {
        "allocate_upload": state.allocate_upload,
        "commit_upload": state.commit_upload,
        "publish_deployment": state.publish_deployment,
        "inspect-run": state.inspect_run,
        "refresh-run": state.refresh_run,
        "read-progress": state.read_progress,
        "cancel-run": state.cancel_run,
        "plan-retry": state.plan_retry,
        "start-retry": state.start_retry,
        "prepare-result": state.prepare_result,
        "plan-cleanup": state.plan_cleanup,
        "apply-cleanup": state.apply_cleanup,
        "validate-deployment": state.validate_deployment,
    }


def handle_request(
    state: GatewayState,
    request: GatewayRequest,
    handlers: Mapping[str, Callable[[Mapping[str, Any]], Mapping[str, Any]]] | None = None,
) -> GatewayResponse:
    """Dispatch one request, journaling any request carrying an operation ID."""
    active_handlers = _default_handlers(state)
    if handlers is not None:
        active_handlers.update(handlers)
    try:
        if request.operation == "capabilities":
            if request.operation_id is not None or request.arguments:
                raise _failure(
                    "protocol-incompatible", "capabilities accepts no arguments."
                )
            result = capabilities()
        elif request.operation == "submit-plan":
            result = state.submit_plan_request(request)
        else:
            try:
                handler = active_handlers[request.operation]
            except KeyError as exc:
                raise _failure(
                    "protocol-incompatible", "The gateway operation is unsupported."
                ) from exc
            if request.operation_id is None:
                result = dict(handler(request.arguments))
            else:
                result = state.mutate(request, handler)
        return GatewayResponse.ok(request.request_id, result)
    except GatewayOperationFailure as exc:
        return GatewayResponse.error(request.request_id, exc.diagnostic)


def run_gateway(
    state: GatewayState,
    encoded: bytes,
    handlers: Mapping[str, Callable[[Mapping[str, Any]], Mapping[str, Any]]] | None = None,
) -> bytes:
    """Validate and execute one bounded gateway request."""
    try:
        request = GatewayRequest.decode(encoded)
    except GatewayProtocolError:
        # A trustworthy response cannot echo an unvalidated request ID.  Stable
        # entry wrappers should log this locally and return a non-zero status.
        raise
    return handle_request(state, request, handlers).encode()


def main() -> int:
    """Gateway console entry used by an immutable stable dispatcher."""
    root = os.environ.get("BIOIMAGEFLOW_CLUSTER_ROOT")
    if root is None:
        return 2
    try:
        state = GatewayState(root)
        encoded = sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1)
        response = run_gateway(state, encoded)
    except (GatewayProtocolError, GatewayOperationFailure):
        return 2
    sys.stdout.buffer.write(response)
    sys.stdout.buffer.write(b"\n")
    sys.stdout.buffer.flush()
    return 0


__all__ = [
    "RECEIPT_SCHEMA",
    "ROOT_NAMESPACES",
    "GatewayOperationFailure",
    "GatewayState",
    "capabilities",
    "handle_request",
    "run_gateway",
]


if __name__ == "__main__":
    raise SystemExit(main())
