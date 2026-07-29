from __future__ import annotations

import shutil
import uuid
from pathlib import Path

import pytest

from bioimageflow.launcher.cluster_bundle import MANIFEST_SCHEMA
from bioimageflow.launcher.cluster_protocol import ClusterProtocolFailure
from bioimageflow.launcher.cluster_upload import allocate_upload, commit_upload
from bioimageflow.storage import canonical_json_bytes
from bioimageflow.storage.dataframe_transport import file_sha256


def _manifest(source: Path) -> dict:
    entries = [
        {
            "digest": (
                file_sha256(path)
                if path.is_file()
                else "sha256:"
                + "e3b0c44298fc1c149afbf4c8996fb924"
                + "27ae41e4649b934ca495991b7852b855"
            ),
            "kind": "file" if path.is_file() else "directory",
            "path": path.relative_to(source).as_posix(),
            "size": path.stat().st_size if path.is_file() else 0,
        }
        for path in sorted(source.rglob("*"))
    ]
    body = {
        "entries": entries,
        "root_name": "submission",
        "schema": MANIFEST_SCHEMA,
    }
    import hashlib

    return {
        **body,
        "digest": f"sha256:{hashlib.sha256(canonical_json_bytes(body)).hexdigest()}",
    }


def test_allocate_and_commit_are_idempotent_and_content_addressed(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "request.json").write_text("{}")
    (source / "nested").mkdir()
    (source / "nested" / "data.bin").write_bytes(b"content")
    manifest = _manifest(source)
    staging = tmp_path / "staging"
    allocate_id = str(uuid.uuid4())

    first = allocate_upload(staging, allocate_id, "sha256:a", manifest)
    second = allocate_upload(staging, allocate_id, "sha256:a", manifest)
    assert first == second

    shutil.copytree(source, Path(first["remote_root"]), dirs_exist_ok=True)
    commit_id = str(uuid.uuid4())
    committed = commit_upload(
        staging,
        commit_id,
        "sha256:b",
        first["upload_id"],
        manifest,
    )
    repeated = commit_upload(
        staging,
        commit_id,
        "sha256:b",
        first["upload_id"],
        manifest,
    )

    assert committed == repeated
    assert manifest["digest"].removeprefix("sha256:") in committed["object_path"]
    assert Path(committed["object_path"]).is_dir()
    assert first["upload_id"] not in committed["object_path"]


def test_request_id_digest_conflict_fails(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "request.json").write_text("{}")
    manifest = _manifest(source)
    request_id = str(uuid.uuid4())
    allocate_upload(tmp_path / "staging", request_id, "sha256:a", manifest)

    with pytest.raises(ClusterProtocolFailure) as captured:
        allocate_upload(
            tmp_path / "staging",
            request_id,
            "sha256:different",
            manifest,
        )

    assert captured.value.code == "duplicate-request-conflict"


def test_commit_rejects_missing_extra_and_tampered_files(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "request.json").write_text("{}")
    manifest = _manifest(source)
    staging = tmp_path / "staging"
    allocated = allocate_upload(
        staging,
        str(uuid.uuid4()),
        "sha256:a",
        manifest,
    )
    partial = Path(allocated["remote_root"])
    (partial / "request.json").write_text("tampered")
    (partial / "extra").write_text("extra")

    with pytest.raises(ClusterProtocolFailure) as captured:
        commit_upload(
            staging,
            str(uuid.uuid4()),
            "sha256:b",
            allocated["upload_id"],
            manifest,
        )

    assert captured.value.code == "upload-integrity"


def test_commit_rejects_symlinks(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "request.json").write_text("{}")
    manifest = _manifest(source)
    staging = tmp_path / "staging"
    allocated = allocate_upload(
        staging,
        str(uuid.uuid4()),
        "sha256:a",
        manifest,
    )
    partial = Path(allocated["remote_root"])
    (partial / "request.json").symlink_to(source / "request.json")

    with pytest.raises(ClusterProtocolFailure, match="symlink"):
        commit_upload(
            staging,
            str(uuid.uuid4()),
            "sha256:b",
            allocated["upload_id"],
            manifest,
        )
