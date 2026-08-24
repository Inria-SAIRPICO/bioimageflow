"""Build the minimal self-installing gateway artifact shipped over bootstrap."""

from __future__ import annotations

import hashlib
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path


_MAIN = r'''from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import stat
import sys
import uuid
from pathlib import Path

SAFE_ROOT = re.compile(r"^/[A-Za-z0-9._/+:@-]+$")
SAFE_OPERATION = re.compile(r"^[0-9a-f-]{36}$")


def digest(path):
    value = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            value.update(chunk)
    return "sha256:" + value.hexdigest()


def atomic_bytes(path, content, mode):
    temporary = path.with_name("." + path.name + "." + uuid.uuid4().hex + ".partial")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        offset = 0
        while offset < len(content):
            offset += os.write(descriptor, content[offset:])
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.replace(temporary, path)
    os.chmod(path, mode)
    parent = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(parent)
    finally:
        os.close(parent)


def install(root_text, operation_id):
    if SAFE_ROOT.fullmatch(root_text) is None or SAFE_OPERATION.fullmatch(operation_id) is None:
        raise SystemExit(2)
    root = Path(root_text)
    gateway = root / "gateway"
    candidate = root / "temporary" / ("bootstrap-" + operation_id)
    setup_source = candidate / "setup.sh"
    source = Path(sys.argv[0]).resolve()
    for path in (root, gateway, candidate):
        metadata = path.stat(follow_symlinks=False)
        if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.geteuid() or metadata.st_mode & 0o022:
            raise SystemExit(2)
    setup_metadata = setup_source.stat(follow_symlinks=False)
    source_metadata = source.stat(follow_symlinks=False)
    if not stat.S_ISREG(setup_metadata.st_mode) or setup_metadata.st_nlink != 1:
        raise SystemExit(2)
    if not stat.S_ISREG(source_metadata.st_mode) or source_metadata.st_nlink != 1:
        raise SystemExit(2)
    artifact_digest = digest(source)
    setup_digest = digest(setup_source)
    publication_id = artifact_digest[7:]
    publications = gateway / "publications"
    publications.mkdir(mode=0o700, exist_ok=True)
    publication = publications / publication_id
    if publication.exists():
        if digest(publication / "gateway.pyz") != artifact_digest or digest(publication / "setup.sh") != setup_digest:
            raise SystemExit(2)
    else:
        publication_candidate = gateway / (".publication-" + operation_id)
        publication_candidate.mkdir(mode=0o700)
        artifact_target = publication_candidate / "gateway.pyz"
        setup_target = publication_candidate / "setup.sh"
        shutil.copyfile(source, artifact_target, follow_symlinks=False)
        shutil.copyfile(setup_source, setup_target, follow_symlinks=False)
        os.chmod(artifact_target, 0o600)
        os.chmod(setup_target, 0o600)
        manifest = {
            "schema": "bioimageflow.cluster.gateway_publication.v1",
            "publication_id": publication_id,
            "artifact_digest": artifact_digest,
            "setup_digest": setup_digest,
            "python": sys.executable,
            "protocol_versions": [1],
        }
        atomic_bytes(
            publication_candidate / "manifest.json",
            json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode(),
            0o600,
        )
        os.rename(publication_candidate, publication)
    artifact = publication / "gateway.pyz"
    setup = publication / "setup.sh"
    python = Path(sys.executable).resolve()
    entry = """#!/usr/bin/env bash
set -euo pipefail
actual_setup=\"sha256:$(sha256sum -- {setup} | cut -d ' ' -f 1)\"
[[ \"$actual_setup\" == {setup_digest} ]]
source {setup}
actual_artifact=\"sha256:$(sha256sum -- {artifact} | cut -d ' ' -f 1)\"
[[ \"$actual_artifact\" == {artifact_digest} ]]
export BIOIMAGEFLOW_CLUSTER_ROOT={root}
export BIOIMAGEFLOW_GATEWAY_ARTIFACT_DIGEST={artifact_digest}
export BIOIMAGEFLOW_GATEWAY_PUBLICATION_ID={publication_id}
exec {python} {artifact}
""".format(
        setup=shlex.quote(str(setup)),
        setup_digest=shlex.quote(setup_digest),
        artifact=shlex.quote(str(artifact)),
        artifact_digest=shlex.quote(artifact_digest),
        root=shlex.quote(str(root)),
        publication_id=shlex.quote(publication_id),
        python=shlex.quote(str(python)),
    ).encode()
    atomic_bytes(gateway / "entry", entry, 0o700)


if len(sys.argv) == 5 and sys.argv[1] == "--install-root" and sys.argv[3] == "--operation-id":
    install(sys.argv[2], sys.argv[4])
elif len(sys.argv) == 1:
    from bioimageflow.cluster.gateway import main
    raise SystemExit(main())
else:
    raise SystemExit(2)
'''

_STORAGE = r'''from __future__ import annotations

import json


def canonical_json_bytes(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
'''


def _zip_entry(name: str, content: bytes) -> tuple[zipfile.ZipInfo, bytes]:
    entry = zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0))
    entry.create_system = 3
    entry.external_attr = (0o100600 & 0xFFFF) << 16
    entry.compress_type = zipfile.ZIP_DEFLATED
    return entry, content


@dataclass(slots=True)
class GatewayArtifact:
    """Owned local gateway zipapp snapshot."""

    path: Path
    digest: str
    _temporary: tempfile.TemporaryDirectory[str]

    def verify(self) -> None:
        observed = f"sha256:{hashlib.sha256(self.path.read_bytes()).hexdigest()}"
        if observed != self.digest:
            raise RuntimeError("The prepared gateway artifact changed.")

    def close(self) -> None:
        self._temporary.cleanup()

    def __enter__(self) -> "GatewayArtifact":
        self.verify()
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()


def build_gateway_artifact() -> GatewayArtifact:
    """Snapshot a deterministic, standard-library-only gateway zipapp."""
    temporary = tempfile.TemporaryDirectory(prefix="bioimageflow-gateway-")
    path = Path(temporary.name) / "gateway.pyz"
    cluster_root = Path(__file__).parent
    entries = {
        "__main__.py": _MAIN.encode(),
        "bioimageflow/__init__.py": b"",
        "bioimageflow/cluster/__init__.py": b"",
        "bioimageflow/cluster/_common.py": (cluster_root / "_common.py").read_bytes(),
        "bioimageflow/cluster/gateway.py": (cluster_root / "gateway.py").read_bytes(),
        "bioimageflow/cluster/protocol.py": (cluster_root / "protocol.py").read_bytes(),
        "bioimageflow/storage/__init__.py": _STORAGE.encode(),
    }
    try:
        with zipfile.ZipFile(path, "x") as archive:
            for name, content in sorted(entries.items()):
                archive.writestr(*_zip_entry(name, content))
        digest = f"sha256:{hashlib.sha256(path.read_bytes()).hexdigest()}"
        return GatewayArtifact(path, digest, temporary)
    except BaseException:
        temporary.cleanup()
        raise


__all__ = ["GatewayArtifact", "build_gateway_artifact"]
