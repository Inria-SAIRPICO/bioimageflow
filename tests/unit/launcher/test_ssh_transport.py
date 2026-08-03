from __future__ import annotations

import json
import subprocess
import sys
import uuid
from pathlib import Path, PurePosixPath

import pytest

from bioimageflow import RunRetryPlan, SSHSubmissionTransport
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
    upload_id = str(uuid.uuid4())

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
                    "result": {
                        "remote_root": f"/cluster/staging/.partial/{upload_id}",
                        "upload_id": upload_id,
                    },
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


@pytest.mark.parametrize(
    ("operation", "extra_arguments", "extra_result"),
    [
        ("inspect", {}, {}),
        ("refresh", {}, {}),
        ("cancel", {"staging_root": "/cluster/staging"}, {}),
        (
            "read-progress",
            {"after_sequence": 0, "limit": 500},
            {"events": [], "has_more": False, "next_sequence": 0},
        ),
        (
            "read-logs",
            {
                "identity": None,
                "limit": 1024,
                "offset": 0,
                "snapshot_size": None,
                "stream": "stdout",
            },
            {
                "data": "",
                "eof": True,
                "exists": False,
                "identity": None,
                "next_offset": 0,
                "reset": False,
                "snapshot_size": 0,
                "stream": "stdout",
            },
        ),
    ],
)
def test_v3_retry_observations_cross_every_production_transport_path(
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    extra_arguments: dict,
    extra_result: dict,
) -> None:
    request_id = str(uuid.uuid4())
    parent_id = "run_1234567812344abc923456789abcdef0"
    retry_id = "run_1234567812344abc923456789abcdeff"
    storage = "/cluster/results"
    plan = RunRetryPlan(
        parent_run_id=parent_id,
        retry_run_id=retry_id,
        parent_status="failed",
        parent_status_revision=3,
        storage_path=storage,
        retained_submission_digest="sha256:" + "1" * 64,
        retained_material_digest="sha256:" + "2" * 64,
        retained_material_entries=0,
        cache_selection_revision="sha256:" + "3" * 64,
        recompute=None,
        invalidations=(),
        conflicting_run_ids=(),
    )
    result = {
        "error": None,
        "retry_plan": plan.to_dict(),
        "run_id": retry_id,
        "state": "prepared",
        "status_revision": 0,
        "storage_path": storage,
        "submission_schema": "bioimageflow.launcher.submission.v3",
        "status_schema": "bioimageflow.launcher.status.v1",
        "terminal": False,
        "updated_at": "2026-08-03T12:00:00Z",
        **extra_result,
    }
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda argv, **kwargs: subprocess.CompletedProcess(
            argv,
            0,
            stdout=json.dumps(
                {
                    "error": None,
                    "ok": True,
                    "request_id": request_id,
                    "result": result,
                    "schema": RESPONSE_SCHEMA,
                }
            ).encode(),
            stderr=b"",
        ),
    )

    observed = execute_cluster_command(
        _transport(),
        operation,
        {"run_id": retry_id, "storage_path": storage, **extra_arguments},
        request_id=request_id,
    )

    assert observed["retry_plan"] == plan.to_dict()


def test_allocate_response_must_bind_exact_server_partial_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request_id = str(uuid.uuid4())
    upload_id = str(uuid.uuid4())
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda argv, **kwargs: subprocess.CompletedProcess(
            argv,
            0,
            stdout=json.dumps(
                {
                    "error": None,
                    "ok": True,
                    "request_id": request_id,
                    "result": {
                        "remote_root": (
                            f"/cluster/staging/.partial/{upload_id}/nested"
                        ),
                        "upload_id": upload_id,
                    },
                    "schema": RESPONSE_SCHEMA,
                }
            ).encode(),
            stderr=b"",
        ),
    )

    with pytest.raises(SSHTransportError) as captured:
        execute_cluster_command(
            _transport(),
            "allocate-upload",
            {"manifest": {}, "staging_root": "/cluster/staging"},
            request_id=request_id,
        )

    assert captured.value.code == "remote-protocol"
    assert captured.value.ambiguous is True


def test_response_rejects_duplicate_keys_and_non_boolean_retryability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request_id = str(uuid.uuid4())
    encoded = (
        '{"schema":"bioimageflow.cluster.response.v1",'
        f'"request_id":"{request_id}","ok":false,"ok":true,'
        '"result":null,"error":null}'
    ).encode()
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda argv, **kwargs: subprocess.CompletedProcess(
            argv,
            0,
            stdout=encoded,
            stderr=b"",
        ),
    )

    with pytest.raises(SSHTransportError, match="duplicate"):
        execute_cluster_command(
            _transport(),
            "submit",
            {},
            request_id=request_id,
        )


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
    "result": {{
        "remote_root": "/cluster/staging/.partial/00000000-0000-4000-8000-000000000000",
        "upload_id": "00000000-0000-4000-8000-000000000000",
    }},
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
