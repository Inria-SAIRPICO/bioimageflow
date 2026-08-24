from __future__ import annotations

import json
import subprocess
import uuid

import pytest

from bioimageflow.cluster.protocol import GatewayResponse
from bioimageflow.cluster.transport import GatewayClientTransport


def test_transport_uses_safe_argv_minimal_environment_and_exact_stdin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = GatewayClientTransport("alice@hpc", "/cluster/alice/bif", 2.2)
    request_id = str(uuid.uuid4())
    captured = {}

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        captured.update(kwargs)
        response = GatewayResponse.ok(request_id, {"gateway_version": "1.0"})
        return subprocess.CompletedProcess(argv, 0, response.encode() + b"\n", b"secret")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setenv("BIOIMAGEFLOW_SECRET", "must-not-leak")
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/agent.sock")

    result = transport.request("capabilities", {}, request_id=request_id)

    assert result == {"gateway_version": "1.0"}
    assert captured["argv"] == [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=3",
        "--",
        "alice@hpc",
        "/cluster/alice/bif/gateway/entry",
    ]
    assert captured["shell"] is False
    assert captured["env"]["SSH_AUTH_SOCK"] == "/tmp/agent.sock"
    assert "BIOIMAGEFLOW_SECRET" not in captured["env"]
    assert json.loads(captured["input"])["request_id"] == request_id


def test_connection_reports_fresh_root_without_bootstrapping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = GatewayClientTransport("hpc", "/cluster/alice/bif")
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda argv, **kwargs: subprocess.CompletedProcess(argv, 127, b"", b"missing"),
    )

    report = transport.check_connection()

    assert report["reachable"] is True
    assert report["gateway_available"] is False
    assert report["bootstrap_required"] is True


@pytest.mark.parametrize(
    "host",
    ["-oProxyCommand=bad", "ssh://cluster", "cluster extra", "cluster\ncommand"],
)
def test_transport_rejects_unsafe_destination(host: str) -> None:
    with pytest.raises(ValueError):
        GatewayClientTransport(host, "/cluster/alice/bif")
