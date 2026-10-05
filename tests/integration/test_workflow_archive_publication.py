"""Public requested definition effects preserve prior and foreign destinations."""

import json
import zipfile

import pytest

from bioimageflow import Workflow


def _definition(tmp_path, name="definition"):
    return Workflow(name=name, engine="direct", storage_path=tmp_path / "definition-results")


@pytest.mark.parametrize("suffix", [".json", ".zip"])
def test_successful_definition_export_and_load_preserve_graph_and_runtime_storage(tmp_path, suffix):
    definition = _definition(tmp_path)
    destination = tmp_path / f"definition{suffix}"
    definition.export(destination)
    if suffix == ".zip":
        with zipfile.ZipFile(destination) as archive:
            assert archive.namelist() == ["workflow.json"]
            wire = json.loads(archive.read("workflow.json"))
        assert wire == definition.to_archive_dict()
    else:
        assert json.loads(destination.read_text()) == definition.to_dict(include_custom_tools=True)
    runtime = tmp_path / "loaded-results"
    loaded = Workflow.load(destination, storage_path=runtime)
    assert loaded.storage_path == runtime.resolve()
    assert loaded.to_dict() == definition.to_dict()


def test_successful_persistent_import_uses_destination_separate_from_runtime(tmp_path):
    definition = _definition(tmp_path)
    archive = tmp_path / "definition.zip"
    definition.export(archive)
    with zipfile.ZipFile(archive, "a") as exported:
        exported.writestr("tools/", b"")
        exported.writestr("tools/readme.txt", b"ordinary archive directory member")
    destination = tmp_path / "imported"
    runtime = tmp_path / "runtime"
    loaded = Workflow.import_archive(archive, destination, storage_path=runtime)
    assert (destination / "workflow.json").is_file()
    assert (destination / "tools/readme.txt").read_bytes() == b"ordinary archive directory member"
    assert loaded.to_dict() == definition.to_dict()
    assert loaded.storage_path == runtime.resolve()


def test_persistent_import_refuses_preexisting_symlink_ancestor_without_outside_write(tmp_path):
    definition = _definition(tmp_path)
    archive_path = tmp_path / "definition.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("workflow.json", json.dumps(definition.to_archive_dict()))
        archive.writestr("tools/source-asset.txt", b"new archive bytes")
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "source-asset.txt"
    sentinel.write_bytes(b"foreign outside sentinel")
    destination = tmp_path / "existing-import"
    destination.mkdir()
    ancestor = destination / "tools"
    ancestor.symlink_to(outside, target_is_directory=True)
    identities = [(path.stat().st_dev, path.stat().st_ino) for path in (destination, sentinel)]
    link_identity = (ancestor.lstat().st_dev, ancestor.lstat().st_ino)
    with pytest.raises(FileExistsError):
        Workflow.import_archive(archive_path, destination, storage_path=tmp_path / "runtime")
    assert sentinel.read_bytes() == b"foreign outside sentinel"
    assert [(path.stat().st_dev, path.stat().st_ino) for path in (destination, sentinel)] == identities
    assert (ancestor.lstat().st_dev, ancestor.lstat().st_ino) == link_identity
    assert ancestor.is_symlink() and ancestor.resolve() == outside
    assert not (destination / "workflow.json").exists()


def test_zip_export_write_failure_preserves_existing_requested_file(tmp_path, monkeypatch):
    previous = _definition(tmp_path, name="previous")
    requested = tmp_path / "requested.zip"
    previous.export(requested)
    before = requested.read_bytes()
    replacement = _definition(tmp_path, name="replacement")
    original = zipfile.ZipFile.writestr
    primary = OSError("injected failure after real workflow.json ZIP write")
    actual_written = []

    def write_then_fail(self, member, data, *args, **kwargs):
        original(self, member, data, *args, **kwargs)
        actual_written.append(member)
        raise primary

    monkeypatch.setattr(zipfile.ZipFile, "writestr", write_then_fail)
    with pytest.raises(OSError) as caught:
        replacement.export(requested)
    assert caught.value is primary
    assert actual_written == ["workflow.json"]
    assert requested.read_bytes() == before


def test_invalid_later_archive_entry_refuses_before_destination_effects(tmp_path):
    definition = _definition(tmp_path)
    archive_path = tmp_path / "invalid-later.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("workflow.json", json.dumps(definition.to_archive_dict()))
        archive.writestr("tools/earlier.txt", b"earlier safe bytes")
        archive.writestr("../outside.txt", b"invalid later bytes")
    destination = tmp_path / "not-created"
    before = {path.name for path in tmp_path.iterdir()}
    with pytest.raises(ValueError):
        Workflow.import_archive(archive_path, destination, storage_path=tmp_path / "runtime")
    assert not destination.exists()
    assert not (tmp_path / "outside.txt").exists()
    assert {path.name for path in tmp_path.iterdir()} == before


def test_persistent_import_late_empty_winner_is_preserved(tmp_path, monkeypatch):
    import bioimageflow.workflow.loading as loading

    definition = _definition(tmp_path)
    archive_path = tmp_path / "definition.zip"
    definition.export(archive_path)
    destination = tmp_path / "winner"
    before = {path.name for path in tmp_path.iterdir()}
    original = loading.publish_no_replace
    observed = []

    def create_winner_then_publish(source, target):
        assert target == destination
        destination.mkdir()
        observed.append((source, destination.stat().st_dev, destination.stat().st_ino))
        return original(source, target)

    monkeypatch.setattr(loading, "publish_no_replace", create_winner_then_publish)
    with pytest.raises(FileExistsError):
        Workflow.import_archive(archive_path, destination, storage_path=tmp_path / "runtime")
    assert len(observed) == 1
    stage, device, inode = observed[0]
    assert destination.is_dir() and list(destination.iterdir()) == []
    assert (destination.stat().st_dev, destination.stat().st_ino) == (device, inode)
    assert not stage.exists()
    assert {path.name for path in tmp_path.iterdir()} == before | {"winner"}


def test_refused_load_retires_its_owned_temporary_archive_root(tmp_path, monkeypatch):
    import bioimageflow.workflow.loading as loading

    archive_path = tmp_path / "malformed.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("workflow.json", b"not JSON")
    original = loading.tempfile.mkdtemp
    observed = []

    def tracked_mkdtemp(*args, **kwargs):
        from pathlib import Path

        result = Path(original(*args, **kwargs))
        observed.append(result)
        return str(result)

    monkeypatch.setattr(loading.tempfile, "mkdtemp", tracked_mkdtemp)
    with pytest.raises(ValueError):
        Workflow.load(archive_path, storage_path=tmp_path / "runtime")
    assert len(observed) == 1
    assert not observed[0].exists()
