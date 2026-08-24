from __future__ import annotations

import json
import uuid

import pytest

from bioimageflow.cluster.protocol import (
    REQUEST_SCHEMA,
    GatewayProtocolError,
    GatewayRequest,
    GatewayResponse,
)


def test_request_round_trip_and_retry_digest_are_stable() -> None:
    operation_id = str(uuid.uuid4())
    first = GatewayRequest.create(
        "submit_plan",
        {"run_id": "run-1", "values": [1, 2]},
        operation_id=operation_id,
    )
    second = GatewayRequest.create(
        "submit_plan",
        {"run_id": "run-1", "values": [1, 2]},
        operation_id=operation_id,
    )

    assert first.request_id != second.request_id
    assert first.operation_digest == second.operation_digest
    assert GatewayRequest.decode(first.encode()) == first
    assert first.to_dict()["schema"] == REQUEST_SCHEMA


@pytest.mark.parametrize(
    "encoded",
    [
        b'{"schema":1,"schema":2}',
        b'{"value":NaN}',
        b"[]",
        b"\xff",
    ],
)
def test_request_decoder_rejects_ambiguous_json(encoded: bytes) -> None:
    with pytest.raises(GatewayProtocolError):
        GatewayRequest.decode(encoded)


def test_response_requires_a_complete_sanitized_diagnostic() -> None:
    request_id = str(uuid.uuid4())
    diagnostic = {
        "schema": "bioimageflow.cluster_diagnostic.v1",
        "phase": "submission",
        "category": "scheduler-rejected",
        "message": "The scheduler rejected the job.",
        "allocation_state": "none",
        "retry_safety": "safe",
        "next_action": "inspect-job-requirements",
        "identities": {"run_id": "run-1"},
    }
    response = GatewayResponse.error(request_id, diagnostic)

    assert GatewayResponse.decode(response.encode()) == response
    invalid = response.to_dict()
    invalid["diagnostic"] = {"category": "scheduler-rejected"}
    with pytest.raises(GatewayProtocolError):
        GatewayResponse.decode(json.dumps(invalid).encode())
