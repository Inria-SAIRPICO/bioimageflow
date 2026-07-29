from __future__ import annotations

import json
import shutil
import uuid
from pathlib import Path

import pytest

from bioimageflow.launcher.cluster_bundle import MANIFEST_SCHEMA
from bioimageflow.launcher.cluster_protocol import ClusterProtocolFailure
from bioimageflow.launcher.cluster_upload import (
    allocate_upload,
    commit_upload,
    validate_manifest,
)
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


def _redigest(manifest: dict) -> dict:
    import hashlib

    body = {
        "entries": manifest["entries"],
        "root_name": manifest["root_name"],
        "schema": manifest["schema"],
    }
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


def test_operation_receipt_rejects_unknown_or_rebound_fields(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "request.json").write_text("{}")
    staging = tmp_path / "staging"
    request_id = str(uuid.uuid4())
    manifest = _manifest(source)
    allocate_upload(staging, request_id, "sha256:a", manifest)
    receipt_path = (
        staging / "receipts" / "allocate-upload" / f"{request_id}.json"
    )
    receipt = json.loads(receipt_path.read_text())
    receipt["unknown"] = True
    receipt_path.write_text(json.dumps(receipt))

    with pytest.raises(ClusterProtocolFailure) as captured:
        allocate_upload(staging, request_id, "sha256:a", manifest)

    assert captured.value.code == "corrupt-receipt"


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


def test_manifest_rejects_noncanonical_order_and_excess_depth(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "a").write_text("a")
    (source / "b").write_text("b")
    manifest = _manifest(source)

    reversed_manifest = _redigest(
        {**manifest, "entries": list(reversed(manifest["entries"]))}
    )
    with pytest.raises(ClusterProtocolFailure, match="canonical"):
        validate_manifest(reversed_manifest)

    [entry] = [
        item for item in manifest["entries"] if item["kind"] == "file"
    ][:1]
    deep_manifest = _redigest(
        {
            **manifest,
            "entries": [
                {
                    **entry,
                    "path": "/".join(["nested"] * 65),
                }
            ],
        }
    )
    with pytest.raises(ClusterProtocolFailure):
        validate_manifest(deep_manifest)


def test_reused_object_must_remain_read_only(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "request.json").write_text("{}")
    manifest = _manifest(source)
    staging = tmp_path / "staging"

    first = allocate_upload(staging, str(uuid.uuid4()), "sha256:a", manifest)
    shutil.copytree(source, Path(first["remote_root"]), dirs_exist_ok=True)
    committed = commit_upload(
        staging,
        str(uuid.uuid4()),
        "sha256:b",
        first["upload_id"],
        manifest,
    )
    object_file = Path(committed["object_path"]) / "request.json"
    object_file.chmod(0o644)

    second = allocate_upload(staging, str(uuid.uuid4()), "sha256:c", manifest)
    shutil.copytree(source, Path(second["remote_root"]), dirs_exist_ok=True)
    with pytest.raises(ClusterProtocolFailure, match="read-only"):
        commit_upload(
            staging,
            str(uuid.uuid4()),
            "sha256:d",
            second["upload_id"],
            manifest,
        )


def test_staging_root_must_not_be_a_symlink(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    staging = tmp_path / "staging"
    staging.symlink_to(target, target_is_directory=True)
    source = tmp_path / "source"
    source.mkdir()
    (source / "request.json").write_text("{}")

    with pytest.raises(ClusterProtocolFailure, match="non-symlink"):
        allocate_upload(
            staging,
            str(uuid.uuid4()),
            "sha256:a",
            _manifest(source),
        )
