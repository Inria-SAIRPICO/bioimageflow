"""Public archive source admission must precede source writes and execution."""
from __future__ import annotations

import base64
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import zipfile

import pytest

from bioimageflow import Workflow


def _single(source_id: str, filename: str, source: str) -> dict:
    return {
        "id": source_id, "module": "custom.fixture", "filename": filename,
        "source_hash": hashlib.sha256(source.encode()).hexdigest(), "source": source,
    }


def _bundle(source_id: str, files: dict[str, bytes], *, root_package: str = "tools") -> dict:
    digest = hashlib.sha256()
    records = []
    for path, data in files.items():
        source_hash = hashlib.sha256(data).hexdigest()
        digest.update(path.encode())
        digest.update(b"\0")
        digest.update(source_hash.encode("ascii"))
        digest.update(b"\0")
        records.append({"path": path, "encoding": "base64", "content": base64.b64encode(data).decode(), "source_hash": source_hash})
    return {"id": source_id, "module": "tools.first", "filename": "first.py", "root_package": root_package, "source_hash": digest.hexdigest(), "files": records}


def _public_load(kind: str, archive: dict, root: Path) -> Workflow:
    if kind == "from_dict":
        return Workflow.from_dict(archive, storage_path=root / "runtime")
    if kind == "json":
        path = root / "workflow.json"
        path.write_text(json.dumps(archive))
        return Workflow.load(path, storage_path=root / "runtime")
    path = root / "workflow.zip"
    with zipfile.ZipFile(path, "w") as output:
        output.writestr("workflow.json", json.dumps(archive))
    return Workflow.import_archive(path, root / "extracted", storage_path=root / "runtime")


@pytest.mark.parametrize(("unsafe", "entry"), [
    ("absolute-filename", "from_dict"),
    ("traversal-filename", "json"),
    ("escaping-bundle-id", "zip"),
    ("later-invalid-bundle-child", "from_dict"),
])
def test_public_archive_rejects_unsafe_sources_before_staging_or_execution(
    unsafe: str, entry: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive = Workflow(name="empty", storage_path=tmp_path / "empty", engine="direct").to_archive_dict()
    staging = tmp_path / "source-staging"
    staging.mkdir()
    outside_file = staging / "victim.py"
    outside_file.write_bytes(b"original outside source bytes\n")
    outside_package = staging / "outside-package"
    outside_package.mkdir()
    package_sentinel = outside_package / "__init__.py"
    package_sentinel.write_bytes(b"original outside package bytes\n")
    before_file, before_package = outside_file.read_bytes(), package_sentinel.read_bytes()
    executed = tmp_path / "executed.txt"
    source = f"from pathlib import Path\nPath({str(executed)!r}).write_text('executed')\n"
    if unsafe == "absolute-filename":
        records = [_single("safe-first", str(outside_file), source)]
    elif unsafe == "traversal-filename":
        records = [_single("safe-first", "../victim.py", source)]
    elif unsafe == "escaping-bundle-id":
        records = [_bundle("../../../outside-package", {"tools/first.py": source.encode()})]
    else:
        records = [_single("safe-first", "first.py", source), _bundle("safe-second", {"tools/first.py": b"VALUE = 1\n", "../victim.py": b"changed\n"})]
    archive["custom_sources"] = records
    created: list[Path] = []
    original_mkdtemp = tempfile.mkdtemp

    def source_mkdtemp(*args, **kwargs):
        if kwargs.get("prefix") == "bioimageflow_custom_tools_":
            kwargs["dir"] = staging
            path = original_mkdtemp(*args, **kwargs)
            created.append(Path(path))
            return path
        return original_mkdtemp(*args, **kwargs)

    monkeypatch.setattr(tempfile, "mkdtemp", source_mkdtemp)
    error = None
    try:
        _public_load(entry, archive, tmp_path)
    except ValueError as caught:
        error = caught
    assert outside_file.read_bytes() == before_file
    assert package_sentinel.read_bytes() == before_package
    assert not executed.exists(), "an earlier source executed before complete source-table admission"
    assert created == [], "custom source staging began before complete source-table admission"
    assert isinstance(error, ValueError)


def test_safe_nested_bundle_helpers_and_assets_round_trip(tmp_path: Path) -> None:
    archive = json.loads(Path("tests/fixtures/unified_workflow_archive.json").read_text())
    old = archive["custom_sources"][0]
    source_id = "étude-01"
    source = "from .helper import VALUE\n" + old["source"].replace('[1]', '[VALUE]')
    bundle = _bundle(source_id, {
        "tools/__init__.py": b"",
        "tools/first.py": source.encode(),
        "tools/helper.py": b"from pathlib import Path\nVALUE = int((Path(__file__).parent / 'data/value.txt').read_text())\n",
        "tools/data/value.txt": b"1",
    }, root_package="")
    archive["custom_sources"][0] = bundle

    def update_reference(value):
        if isinstance(value, dict):
            if value.get("source_module") == old["id"]:
                value["source_module"] = source_id
                value["tool_module"] = "tools.first"
            for child in value.values():
                update_reference(child)
        elif isinstance(value, list):
            for child in value:
                update_reference(child)

    update_reference(archive["workflow"])
    original = deepcopy(archive)
    loaded = _public_load("json", archive, tmp_path)
    assert loaded.compute().iloc[0].to_dict() == {"one": 1, "two": 2}
    normalized = loaded.to_archive_dict()
    restored = Workflow.from_dict(normalized, storage_path=tmp_path / "restored")
    assert restored.to_archive_dict() == normalized
    assert restored.compute().iloc[0].to_dict() == {"one": 1, "two": 2}
    assert archive == original


def test_public_archive_rejects_reserved_device_filename_before_staging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive = Workflow(name="empty", storage_path=tmp_path / "empty", engine="direct").to_archive_dict()
    archive["custom_sources"] = [_single("safe-device", "NUL.py", "VALUE = 1\n")]
    original_mkdtemp = tempfile.mkdtemp

    def no_custom_staging(*args, **kwargs):
        if kwargs.get("prefix") == "bioimageflow_custom_tools_":
            raise AssertionError("reserved device source reached staging")
        return original_mkdtemp(*args, **kwargs)

    monkeypatch.setattr(tempfile, "mkdtemp", no_custom_staging)
    with pytest.raises(ValueError, match="Invalid embedded custom tool path"):
        Workflow.from_dict(archive, storage_path=tmp_path / "runtime", engine="direct")
