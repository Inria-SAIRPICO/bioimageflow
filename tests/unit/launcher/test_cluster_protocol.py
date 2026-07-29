from __future__ import annotations

import json
import uuid

import pytest

from bioimageflow.launcher.cluster_agent import run_agent
from bioimageflow.launcher.cluster_protocol import (
    REQUEST_SCHEMA,
    ClusterProtocolFailure,
    decode_request,
    request,
)


def test_request_round_trip_is_exact_and_finite() -> None:
    request_id = str(uuid.uuid4())
    value = request(
        "allocate-upload",
        {"manifest": {}, "staging_root": "/cluster/staging"},
        request_id=request_id,
    )

    assert value == {
        "arguments": {
            "manifest": {},
            "staging_root": "/cluster/staging",
        },
        "operation": "allocate-upload",
        "request_id": request_id,
        "schema": REQUEST_SCHEMA,
    }


@pytest.mark.parametrize(
    ("encoded", "code"),
    [
        (
            b'{"schema":"bioimageflow.cluster.command.v1","schema":"x"}',
            "duplicate-json-key",
        ),
        (b"\xff", "invalid-utf8"),
        (b'{"value":NaN}', "nonfinite-json"),
        (b"[]", "invalid-request"),
    ],
)
def test_decoder_rejects_malformed_requests(encoded: bytes, code: str) -> None:
    with pytest.raises(ClusterProtocolFailure) as captured:
        decode_request(encoded)

    assert captured.value.code == code


def test_decoder_rejects_unknown_fields_versions_and_operations() -> None:
    base = {
        "arguments": {},
        "operation": "submit",
        "request_id": str(uuid.uuid4()),
        "schema": REQUEST_SCHEMA,
    }
    for mutation in (
        {**base, "extra": True},
        {**base, "schema": "bioimageflow.cluster.command.v2"},
        {**base, "operation": "inspect"},
    ):
        with pytest.raises(ClusterProtocolFailure):
            decode_request(json.dumps(mutation).encode())


def test_agent_returns_stable_structured_error_without_traceback() -> None:
    request_id = str(uuid.uuid4())
    encoded = json.dumps(
        {
            "arguments": {},
            "operation": "submit",
            "request_id": request_id,
            "schema": REQUEST_SCHEMA,
        }
    ).encode()

    response = json.loads(run_agent(encoded))

    assert response["ok"] is False
    assert response["request_id"] == request_id
    assert response["error"]["code"] == "invalid-arguments"
    assert "traceback" not in json.dumps(response).lower()


def test_agent_rejects_future_protocol_without_echoing_untrusted_id() -> None:
    response = json.loads(
        run_agent(
            json.dumps(
                {
                    "arguments": {},
                    "operation": "submit",
                    "request_id": str(uuid.uuid4()),
                    "schema": "bioimageflow.cluster.command.v2",
                }
            ).encode()
        )
    )

    assert response["ok"] is False
    assert response["request_id"] is None
    assert response["error"]["code"] == "unsupported-protocol"
