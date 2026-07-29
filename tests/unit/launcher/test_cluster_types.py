from __future__ import annotations

from pathlib import Path, PurePosixPath

import pytest

from bioimageflow import LocalUpload, SSHSubmissionTransport


def test_cluster_transport_values_are_strict_and_frozen(tmp_path: Path) -> None:
    upload = LocalUpload(tmp_path / "images")
    transport = SSHSubmissionTransport(
        host="alice@hpc-alias",
        staging_root=PurePosixPath("/cluster/staging"),
        remote_executable=PurePosixPath("/cluster/bin/bioimageflow-cluster-agent"),
        connect_timeout=3,
    )

    assert upload.path == tmp_path / "images"
    assert transport.connect_timeout == 3.0
    assert SSHSubmissionTransport.from_dict(transport.to_dict()) == transport
    with pytest.raises(AttributeError):
        transport.host = "other"  # type: ignore[misc]


@pytest.mark.parametrize(
    "host",
    ["-oProxyCommand=x", "host name", "host\nname", "", "@host", "user@"],
)
def test_cluster_transport_rejects_unsafe_hosts(host: str) -> None:
    with pytest.raises(ValueError):
        SSHSubmissionTransport(
            host=host,
            staging_root=PurePosixPath("/cluster/staging"),
            remote_executable=PurePosixPath("/cluster/bin/agent"),
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("staging_root", PurePosixPath("relative")),
        ("staging_root", PurePosixPath("/cluster/../staging")),
        ("remote_executable", PurePosixPath("/cluster/bin/agent command")),
        ("remote_executable", PurePosixPath("agent")),
    ],
)
def test_cluster_transport_rejects_unsafe_paths(field: str, value: PurePosixPath) -> None:
    values = {
        "host": "hpc",
        "staging_root": PurePosixPath("/cluster/staging"),
        "remote_executable": PurePosixPath("/cluster/bin/agent"),
    }
    values[field] = value

    with pytest.raises(ValueError):
        SSHSubmissionTransport(**values)  # type: ignore[arg-type]


def test_local_upload_requires_exact_path_type() -> None:
    with pytest.raises(TypeError):
        LocalUpload("images")  # type: ignore[arg-type]
