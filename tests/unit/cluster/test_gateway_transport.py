from __future__ import annotations

import json
import subprocess
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from bioimageflow.cluster import gateway_artifact as gateway_artifact_module
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


def test_transport_returns_final_managed_uv_deployment_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "prepared"
    source.mkdir()
    (source / "deployment-manifest.json").write_text("{}", encoding="utf-8")
    prepared_id = "sha256:" + "1" * 64
    final_id = "sha256:" + "2" * 64
    prepared = SimpleNamespace(
        root=source,
        deployment_id=prepared_id,
        manifest={"manifest_digest": prepared_id, "setup": None},
        verify=lambda: None,
    )
    artifact = SimpleNamespace(
        digest="sha256:" + "3" * 64,
        path=tmp_path / "gateway.pyz",
        verify=lambda: None,
        close=lambda: None,
    )
    monkeypatch.setattr(
        gateway_artifact_module, "build_gateway_artifact", lambda: artifact
    )
    transport = GatewayClientTransport("hpc", "/cluster/alice/bif")
    monkeypatch.setattr(
        transport,
        "check_connection",
        lambda: {"bootstrap_required": False, "gateway_available": True},
    )
    monkeypatch.setattr(transport, "upload_file", lambda source, destination: None)

    def request(operation, arguments, operation_id=None, **kwargs):
        if operation == "allocate_upload":
            return {"upload_path": "/cluster/upload", "upload_token": "a" * 32}
        if operation == "commit_upload":
            return {"object_id": kwargs["payload_digest"]}
        assert operation == "publish_deployment"
        assert arguments["deployment_id"] == prepared_id
        return {
            "deployment_id": final_id,
            "manifest_digest": prepared_id,
            "environment_installed": True,
            "environment_kind": "uv",
            "external_attestation_digest": None,
            "reused": False,
            "gateway_publication_id": "gateway-v1",
        }

    monkeypatch.setattr(transport, "request", request)

    result = transport.publish_deployment(prepared)

    assert result["deployment_id"] == final_id
    assert result["external_attestation_digest"] is None


def test_submit_plan_snapshots_before_network_and_resumes_one_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    invocation = tmp_path / "invocation"
    invocation.mkdir()
    (invocation / "invocation.json").write_text("{}")

    class Prepared:
        root = invocation
        invocation_digest = "sha256:" + "1" * 64
        verify_count = 0

        def _verify(self) -> None:
            self.verify_count += 1

        def to_dict(self) -> dict[str, Any]:
            return {"schema": "test.invocation.v1"}

    prepared = Prepared()
    plan = SimpleNamespace(
        attempt_id=str(uuid.uuid4()),
        invocation_digest=prepared.invocation_digest,
        _verify_digest=lambda: None,
        to_dict=lambda: {"schema": "test.plan.v1"},
    )
    transport = GatewayClientTransport("hpc", "/cluster/alice/bif")
    requests: list[tuple[str, str | None]] = []

    def request(
        operation: str,
        arguments: dict[str, Any],
        operation_id: str | None = None,
        **_kwargs: Any,
    ) -> dict[str, Any]:
        assert prepared.verify_count >= 2
        requests.append((operation, operation_id))
        if operation == "submit-plan":
            completed = sum(name == "submit-plan" for name, _item in requests) > 1
            return {
                "run_id": "run",
                "observation": {"state": "starting"},
                "upload_required": not completed,
            }
        if operation == "allocate_upload":
            return {"upload_path": "/cluster/alice/bif/temporary/upload", "upload_token": "a" * 32}
        return {"object_id": arguments["digest"]}

    uploaded: list[Path] = []
    monkeypatch.setattr(transport, "request", request)
    monkeypatch.setattr(
        transport, "upload_file", lambda source, _remote: uploaded.append(Path(source))
    )

    result = transport.submit_plan(plan, prepared)

    assert result["upload_required"] is False
    assert uploaded and uploaded[0].is_file() is False  # private snapshot was released
    assert requests == [
        ("submit-plan", plan.attempt_id),
        ("allocate_upload", f"{plan.attempt_id}:allocate"),
        ("commit_upload", f"{plan.attempt_id}:commit"),
        ("submit-plan", plan.attempt_id),
    ]
