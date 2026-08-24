from __future__ import annotations

import json
import shutil
import subprocess
import uuid
from pathlib import Path

import pytest

from bioimageflow.cluster.bootstrap import (
    BOOTSTRAP_REMOTE_COMMAND,
    BOOTSTRAP_SCHEMA,
    BootstrapClient,
)
from bioimageflow.cluster.gateway import GatewayState
from bioimageflow.cluster.gateway_artifact import build_gateway_artifact
from bioimageflow.cluster.protocol import GatewayRequest, GatewayResponse


def test_probe_uses_constant_command_and_data_only_stdin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = BootstrapClient("alice@hpc", "/cluster/alice/bif", 2.2)
    captured = {}

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        captured.update(kwargs)
        return subprocess.CompletedProcess(
            argv,
            0,
            json.dumps(
                {
                    "schema": BOOTSTRAP_SCHEMA,
                    "status": "bootstrap-required",
                    "upload_path": None,
                }
            ).encode(),
            b"private diagnostic",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    response = client.probe()

    assert response["status"] == "bootstrap-required"
    assert captured["argv"][-1] == BOOTSTRAP_REMOTE_COMMAND
    assert "/cluster/alice/bif" not in BOOTSTRAP_REMOTE_COMMAND
    assert b"/cluster/alice/bif" in captured["input"]
    assert captured["shell"] is False
    assert captured["argv"][:6] == [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=3",
        "--",
    ]


def test_probe_rejects_duplicate_response_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = BootstrapClient("hpc", "/cluster/alice/bif")
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda argv, **kwargs: subprocess.CompletedProcess(
            argv,
            0,
            b'{"schema":"bioimageflow.cluster.bootstrap.v1","schema":"x",'
            b'"status":"bootstrap-required","upload_path":null}',
            b"",
        ),
    )

    with pytest.raises(RuntimeError, match="bootstrap response is invalid"):
        client.probe()


def test_minimal_artifact_installs_digest_checking_stable_entry(
    tmp_path: Path,
) -> None:
    root = tmp_path / "cluster-root"
    GatewayState.initialize(root)
    operation_id = str(uuid.uuid4())
    candidate = root / "temporary" / f"bootstrap-{operation_id}"
    candidate.mkdir(mode=0o700)
    setup = candidate / "setup.sh"
    setup.write_bytes(b"true\n")
    setup.chmod(0o600)

    with build_gateway_artifact() as artifact:
        remote_artifact = candidate / "gateway.pyz"
        shutil.copyfile(artifact.path, remote_artifact)
        remote_artifact.chmod(0o600)
        installed = subprocess.run(
            [
                "python",
                str(remote_artifact),
                "--install-root",
                str(root),
                "--operation-id",
                operation_id,
            ],
            capture_output=True,
            check=False,
        )
        assert installed.returncode == 0
        request = GatewayRequest.create("capabilities", {})
        invoked = subprocess.run(
            [str(root / "gateway" / "entry")],
            input=request.encode(),
            capture_output=True,
            check=False,
        )
        response = GatewayResponse.decode(invoked.stdout.rstrip(b"\n"))

        assert invoked.returncode == 0
        assert response.payload["gateway_artifact_digest"] == artifact.digest
        assert (root / "gateway" / "entry").stat().st_mode & 0o777 == 0o700


def test_gateway_artifact_is_deterministic() -> None:
    with build_gateway_artifact() as first, build_gateway_artifact() as second:
        assert first.digest == second.digest
        assert first.path.read_bytes() == second.path.read_bytes()
