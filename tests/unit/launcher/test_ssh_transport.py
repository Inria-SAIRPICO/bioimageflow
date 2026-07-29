from __future__ import annotations

import json
import subprocess
import sys
import uuid
from pathlib import Path, PurePosixPath

import pytest

from bioimageflow import SSHSubmissionTransport
from bioimageflow.launcher.cluster_bundle import PreparedClusterBundle
from bioimageflow.launcher.cluster_protocol import RESPONSE_SCHEMA
from bioimageflow.launcher.ssh import (
    SSHTransportError,
    execute_cluster_command,
    upload_bundle,
)


def _transport() -> SSHSubmissionTransport:
    return SSHSubmissionTransport(
        host="alice@hpc-alias",
        staging_root=PurePosixPath("/cluster/staging"),
        remote_executable=PurePosixPath("/cluster/bin/bioimageflow-cluster-agent"),
        connect_timeout=2.2,
    )


def test_ssh_uses_exact_safe_argv_environment_and_stdin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = {}
    request_id = str(uuid.uuid4())

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        captured.update(kwargs)
        return subprocess.CompletedProcess(
            argv,
            0,
            stdout=json.dumps(
                {
                    "error": None,
                    "ok": True,
                    "request_id": request_id,
                    "result": {"upload_id": str(uuid.uuid4())},
                    "schema": RESPONSE_SCHEMA,
                }
            ).encode(),
            stderr=b"diagnostic",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setenv("BIOIMAGEFLOW_SECRET", "do-not-forward")
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/agent.sock")

    execute_cluster_command(
        _transport(),
        "allocate-upload",
        {"manifest": {}, "staging_root": "/cluster/staging"},
        request_id=request_id,
    )

    assert captured["argv"] == [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=3",
        "--",
        "alice@hpc-alias",
        "/cluster/bin/bioimageflow-cluster-agent",
    ]
    assert captured["shell"] is False
    assert captured["timeout"] == 2.2
    assert captured["env"]["SSH_AUTH_SOCK"] == "/tmp/agent.sock"
    assert "BIOIMAGEFLOW_SECRET" not in captured["env"]
    envelope = json.loads(captured["input"])
    assert envelope["request_id"] == request_id
    assert "diagnostic" not in envelope


@pytest.mark.parametrize(
    ("failure", "code"),
    [
        (subprocess.TimeoutExpired(["ssh"], 1), "ssh-timeout"),
        (FileNotFoundError(), "ssh-unavailable"),
    ],
)
def test_ssh_classifies_process_failures(
    monkeypatch: pytest.MonkeyPatch,
    failure: Exception,
    code: str,
) -> None:
    def fake_run(*args, **kwargs):
        raise failure

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(SSHTransportError) as captured:
        execute_cluster_command(
            _transport(),
            "submit",
            {},
            request_id=str(uuid.uuid4()),
        )

    assert captured.value.code == code


def test_ssh_classifies_auth_host_key_or_remote_nonzero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda argv, **kwargs: subprocess.CompletedProcess(
            argv,
            255,
            stdout=b"",
            stderr=b"Host key verification failed.",
        ),
    )

    with pytest.raises(SSHTransportError) as captured:
        execute_cluster_command(
            _transport(),
            "submit",
            {},
            request_id=str(uuid.uuid4()),
        )

    assert captured.value.code == "ssh-host-key"
    assert "Host key" not in str(captured.value)


def test_sftp_quotes_every_path_and_uses_server_partial_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    local = tmp_path / 'space "quote" ünicode'
    local.write_bytes(b"content")
    bundle = PreparedClusterBundle(
        root=tmp_path,
        manifest={
            "entries": [
                {
                    "digest": "sha256:" + "0" * 64,
                    "kind": "file",
                    "path": local.name,
                    "size": 7,
                }
            ],
            "digest": "sha256:" + "1" * 64,
        },
    )
    captured = {}

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        captured.update(kwargs)
        return subprocess.CompletedProcess(argv, 0, stdout=b"", stderr=b"")

    monkeypatch.setattr(subprocess, "run", fake_run)

    upload_bundle(
        _transport(),
        bundle,
        "/cluster/staging/.partial/00000000-0000-4000-8000-000000000000",
    )

    assert captured["argv"][0] == "sftp"
    assert captured["shell"] is False
    batch = captured["input"].decode()
    assert batch.startswith('put "')
    assert '\\"quote\\"' in batch
    assert "/cluster/staging/.partial/" in batch


def test_sftp_rejects_server_path_outside_allocated_namespace(
    tmp_path: Path,
) -> None:
    bundle = PreparedClusterBundle(
        root=tmp_path,
        manifest={"entries": [], "digest": "sha256:" + "0" * 64},
    )

    with pytest.raises(SSHTransportError) as captured:
        upload_bundle(_transport(), bundle, "/cluster/results")

    assert captured.value.code == "unsafe-upload-target"


def test_response_loss_is_ambiguous(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda argv, **kwargs: subprocess.CompletedProcess(
            argv,
            0,
            stdout=b"not json",
            stderr=b"",
        ),
    )

    with pytest.raises(SSHTransportError) as captured:
        execute_cluster_command(
            _transport(),
            "submit",
            {},
            request_id=str(uuid.uuid4()),
        )

    assert captured.value.code == "remote-protocol"
    assert captured.value.ambiguous is True


def test_real_fake_ssh_executable_captures_process_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    capture = tmp_path / "ssh-capture.json"
    executable = tmp_path / "ssh"
    executable.write_text(
        f"""#!{sys.executable}
import json
import os
import sys
request = json.loads(sys.stdin.buffer.read())
with open({str(capture)!r}, "w", encoding="utf-8") as stream:
    json.dump({{"argv": sys.argv[1:], "env": dict(os.environ), "request": request}}, stream)
response = {{
    "error": None,
    "ok": True,
    "request_id": request["request_id"],
    "result": {{"upload_id": "00000000-0000-4000-8000-000000000000"}},
    "schema": {RESPONSE_SCHEMA!r},
}}
sys.stdout.write(json.dumps(response))
"""
    )
    executable.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setenv("DO_NOT_FORWARD", "secret")
    request_id = str(uuid.uuid4())

    execute_cluster_command(
        _transport(),
        "allocate-upload",
        {"manifest": {}, "staging_root": "/cluster/staging"},
        request_id=request_id,
    )

    captured = json.loads(capture.read_text())
    assert captured["argv"] == [
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=3",
        "--",
        "alice@hpc-alias",
        "/cluster/bin/bioimageflow-cluster-agent",
    ]
    assert captured["request"]["request_id"] == request_id
    assert "DO_NOT_FORWARD" not in captured["env"]


def test_real_fake_sftp_executable_captures_quoted_batch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    capture = tmp_path / "sftp-capture.json"
    executable = tmp_path / "sftp"
    executable.write_text(
        f"""#!{sys.executable}
import json
import sys
with open({str(capture)!r}, "w", encoding="utf-8") as stream:
    json.dump({{"argv": sys.argv[1:], "batch": sys.stdin.read()}}, stream)
"""
    )
    executable.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))
    local = tmp_path / "file with spaces.txt"
    local.write_text("content")
    bundle = PreparedClusterBundle(
        root=tmp_path,
        manifest={
            "entries": [
                {
                    "digest": "sha256:" + "0" * 64,
                    "kind": "file",
                    "path": local.name,
                    "size": 7,
                }
            ],
            "digest": "sha256:" + "1" * 64,
        },
    )

    upload_bundle(
        _transport(),
        bundle,
        "/cluster/staging/.partial/00000000-0000-4000-8000-000000000000",
    )

    captured = json.loads(capture.read_text())
    assert captured["argv"][-2:] == ["--", "alice@hpc-alias"]
    assert 'file with spaces.txt"' in captured["batch"]
    assert "/cluster/staging/.partial/" in captured["batch"]
