"""Native local bindings remain distinct from POSIX remote bindings."""

import hashlib
import json
from pathlib import PurePosixPath, PureWindowsPath
from uuid import uuid4

import pytest

from bioimageflow.launcher import result_download
from bioimageflow.launcher.errors import WorkflowResultIntegrityError
from bioimageflow.launcher.result_bundle import RESULT_BUNDLE_SCHEMA
from bioimageflow.launcher.schemas import RETURN_SCHEMA
from bioimageflow.storage import canonical_json_bytes


def _manifest(path, storage):
    run_id = "run_" + uuid4().hex
    body = {
        "schema": RESULT_BUNDLE_SCHEMA, "run_id": run_id, "storage_path": storage,
        "entries": [], "record_assets": [],
        "return_manifest": {
            "schema": RETURN_SCHEMA, "run_id": run_id, "shape": "mapping",
            "mapping_keys": [], "frames": [], "root_outputs": [], "locators": [],
        },
    }
    body["digest"] = "sha256:" + hashlib.sha256(canonical_json_bytes(body)).hexdigest()
    path.write_text(json.dumps(body), encoding="utf-8")
    return body


@pytest.mark.parametrize("flavour,storage", [
    (PureWindowsPath, "C:/analysis/results"),
    (PureWindowsPath, "//?/C:/analysis/results"),
    (PurePosixPath, "/analysis/results"),
])
def test_native_local_manifest_accepts_canonical_host_binding(tmp_path, monkeypatch, flavour, storage):
    path = tmp_path / "manifest.json"
    expected = _manifest(path, storage)
    monkeypatch.setattr(result_download, "Path", flavour)
    assert result_download._load_manifest(path, native_storage=True) == expected


@pytest.mark.parametrize("storage", ["C:/analysis/results", "//?/C:/analysis/results", "//server/share"])
def test_remote_manifest_keeps_posix_only_binding(tmp_path, storage):
    path = tmp_path / "manifest.json"
    _manifest(path, storage)
    with pytest.raises(WorkflowResultIntegrityError, match="run binding"):
        result_download._load_manifest(path)


@pytest.mark.parametrize("storage", ["C:results", "results", "C:\\analysis\\results"])
def test_native_manifest_rejects_relative_or_noncanonical_binding(tmp_path, monkeypatch, storage):
    path = tmp_path / "manifest.json"
    _manifest(path, storage)
    monkeypatch.setattr(result_download, "Path", PureWindowsPath)
    with pytest.raises(WorkflowResultIntegrityError, match="run binding"):
        result_download._load_manifest(path, native_storage=True)
