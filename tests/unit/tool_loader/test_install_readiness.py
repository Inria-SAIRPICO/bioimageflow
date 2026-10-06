"""Public installation readiness and ownership, without a network installer."""

import base64
import csv
import hashlib
from io import StringIO
from pathlib import Path
import subprocess

import pytest

from bioimageflow import ToolRegistry
from bioimageflow import tool_loader
from bioimageflow_core import Arguments


def _write_distribution(target, package, *, version="1.2.3", metadata=True, import_marker=None, value=4):
    package_dir = target / package
    package_dir.mkdir(parents=True, exist_ok=True)
    prefix = "" if import_marker is None else f"from pathlib import Path\nPath({str(import_marker)!r}).write_text('imported')\n"
    (package_dir / "__init__.py").write_text(prefix + '''from bioimageflow_core import EnvironmentSpec, IOModel, ProcessingTool, RowConsumption
class ReadyTool(ProcessingTool):
    row_consumption = RowConsumption.MAPPED
    environment = EnvironmentSpec("ready", {})
    class Inputs(IOModel): pass
    class Outputs(IOModel): value: int
    def process_row(self, arguments): return self.Outputs(value=4)
'''.replace("value=4", f"value={value}"))
    if metadata:
        info = target / f"{package}-{version}.dist-info"
        info.mkdir()
        (info / "METADATA").write_text(
            f"Metadata-Version: 2.1\nName: {package.replace('_', '-')}\nVersion: {version}\n"
        )
        (info / "top_level.txt").write_text(package + "\n")
        record = StringIO()
        writer = csv.writer(record)
        for path in sorted(target.rglob("*")):
            if path.is_file():
                content = path.read_bytes()
                digest = base64.urlsafe_b64encode(hashlib.sha256(content).digest()).decode().rstrip("=")
                writer.writerow((path.relative_to(target).as_posix(), "sha256=" + digest, len(content)))
        writer.writerow(((info / "RECORD").relative_to(target).as_posix(), "", ""))
        (info / "RECORD").write_text(record.getvalue())


def _package(tmp_path):
    return "readiness_" + tmp_path.name.replace("-", "_")


@pytest.mark.parametrize("version", ["1.2.3", "1.2.3rc1+local.1", "1!1.2.3rc1+local.1"])
def test_successful_install_registers_actual_matching_distribution(tmp_path, monkeypatch, version):
    package = _package(tmp_path)
    calls = []
    def install(command, **kwargs):
        calls.append(command)
        _write_distribution(Path(command[command.index("--target") + 1]), package, version=version)
    monkeypatch.setattr(tool_loader.subprocess, "run", install)
    registry = ToolRegistry(store_path=tmp_path)
    registry.install_package(package, version)
    try:
        metadata = registry.register_package(package, version)
        selected = registry.get_class("ReadyTool", package=package, version=version)
        assert selected is not None
        assert selected().process_row(Arguments()).value == 4
        assert [item.class_name for item in metadata] == ["ReadyTool"]
        assert metadata[0].version == version
        registry.install_package(package, version)
        assert len(calls) == 1
    finally:
        tool_loader.unload_versioned_package(package, version)


def test_epoch_and_local_versions_keep_actual_callbacks_independent(tmp_path, monkeypatch):
    package = _package(tmp_path)
    versions = {"1.2.3rc1+local.1": 13, "1!1.2.3rc1+local.1": 29}

    def install(command, **kwargs):
        target = Path(command[command.index("--target") + 1])
        version = command[-1].split("==", 1)[1]
        _write_distribution(target, package, version=version, value=versions[version])

    monkeypatch.setattr(tool_loader.subprocess, "run", install)
    registry = ToolRegistry(store_path=tmp_path)
    selected = []
    try:
        for version, expected in versions.items():
            registry.install_package(package, version)
            registry.register_package(package, version)
            tool = registry.get_class("ReadyTool", package=package, version=version)
            assert tool is not None
            selected.append(tool)
            assert tool().process_row(Arguments()).value == expected
        assert selected[0] is not selected[1]
        assert [tool().process_row(Arguments()).value for tool in selected] == [13, 29]
    finally:
        for version in versions:
            tool_loader.unload_versioned_package(package, version)


@pytest.mark.parametrize("package,version", [
    ("../outside", "1.2.3"),
    ("valid_tools", "../../outside"),
    ("valid_tools", "not-a-version"),
])
def test_invalid_selectors_refuse_before_filesystem_or_pip(tmp_path, monkeypatch, package, version):
    store = tmp_path / "store"
    sentinel = tmp_path / "foreign-sentinel"
    sentinel.write_text("preserve")
    before = tuple(sorted(path.relative_to(tmp_path).as_posix() for path in tmp_path.rglob("*")))
    def forbidden_install(command, **kwargs):
        pytest.fail("Invalid selectors reached pip")
    monkeypatch.setattr(tool_loader.subprocess, "run", forbidden_install)
    with pytest.raises(ValueError):
        ToolRegistry(store_path=store).install_package(package, version)
    assert tuple(sorted(path.relative_to(tmp_path).as_posix() for path in tmp_path.rglob("*"))) == before
    assert sentinel.read_text() == "preserve"


def test_child_symlink_cannot_redirect_installation_outside_store(tmp_path, monkeypatch):
    package = _package(tmp_path)
    store = tmp_path / "store"
    outside = tmp_path / "outside"
    store.mkdir()
    outside.mkdir()
    sentinel = outside / "foreign-sentinel"
    sentinel.write_text("preserve")
    child = store / package
    child.symlink_to(outside, target_is_directory=True)
    def forbidden_install(command, **kwargs):
        pytest.fail("Escaping child link reached pip")
    monkeypatch.setattr(tool_loader.subprocess, "run", forbidden_install)
    with pytest.raises(ValueError, match="escapes selected store"):
        ToolRegistry(store_path=store).install_package(package, "1.2.3")
    assert child.is_symlink()
    assert list(outside.iterdir()) == [sentinel]
    assert sentinel.read_text() == "preserve"


@pytest.mark.parametrize("metadata", [False, "wrong-version"], ids=["missing-metadata", "wrong-version"])
def test_half_installed_directory_is_not_a_ready_distribution(tmp_path, monkeypatch, metadata):
    package = _package(tmp_path)
    target = tmp_path / package / "1.2.3"
    import_marker = tmp_path / "invalid-target-imported"
    _write_distribution(target, package, version="9.9.9", metadata=bool(metadata), import_marker=import_marker)
    sentinel = target / "preexisting-unrelated-file"
    sentinel.write_text("preserve")
    calls = []
    def forbidden_repair(command, **kwargs):
        calls.append(command)
        pytest.fail("Occupied invalid target reached the installer")
    monkeypatch.setattr(tool_loader.subprocess, "run", forbidden_repair)
    registry = ToolRegistry(store_path=tmp_path)
    before = {path.relative_to(target): path.read_bytes() for path in target.rglob("*") if path.is_file()}
    with pytest.raises(ValueError) as refusal:
        registry.install_package(package, "1.2.3")
    assert str(target) in str(refusal.value)
    assert package in str(refusal.value) and "1.2.3" in str(refusal.value)
    assert calls == []
    assert registry.list_tools() == []
    assert not import_marker.exists()
    assert {path.relative_to(target): path.read_bytes() for path in target.rglob("*") if path.is_file()} == before


def test_foreign_occupied_target_refuses_without_running_pip(tmp_path, monkeypatch):
    package = _package(tmp_path)
    target = tmp_path / package / "1.2.3"
    target.mkdir(parents=True)
    sentinel = target / "preexisting-unrelated-file"
    sentinel.write_text("preserve")
    calls = []
    def forbidden_install(command, **kwargs):
        calls.append(command)
        pytest.fail("Foreign occupied target reached the installer")
    monkeypatch.setattr(tool_loader.subprocess, "run", forbidden_install)
    with pytest.raises(ValueError) as refusal:
        ToolRegistry(store_path=tmp_path).install_package(package, "1.2.3")
    assert str(target) in str(refusal.value)
    assert package in str(refusal.value) and "1.2.3" in str(refusal.value)
    assert calls == []
    assert sentinel.read_text() == "preserve"
    assert not (target / package).exists()


def test_fresh_failed_install_cleans_owned_stage_and_preserves_primary_error(tmp_path, monkeypatch):
    package = _package(tmp_path)
    target = tmp_path / package / "1.2.3"
    unrelated = tmp_path / "unrelated" / "preexisting-file"
    unrelated.parent.mkdir()
    unrelated.write_text("preserve")
    stages = []
    def failed_install(command, **kwargs):
        stage = Path(command[command.index("--target") + 1])
        stages.append(stage)
        partial = stage / package
        partial.mkdir(parents=True, exist_ok=True)
        (partial / "__init__.py").write_text("PARTIAL = True\n")
        raise subprocess.CalledProcessError(1, command, stderr="deliberate install failure")
    monkeypatch.setattr(tool_loader.subprocess, "run", failed_install)
    with pytest.raises(RuntimeError, match="deliberate install failure") as failure:
        ToolRegistry(store_path=tmp_path).install_package(package, "1.2.3")
    assert isinstance(failure.value.__cause__, subprocess.CalledProcessError)
    assert failure.value.__cause__.stderr == "deliberate install failure"
    assert len(stages) == 1 and stages[0] != target
    assert not stages[0].exists()
    assert not target.exists()
    assert unrelated.read_text() == "preserve"


def test_unrecorded_staged_import_member_is_refused_before_publication(tmp_path, monkeypatch):
    package = _package(tmp_path)
    target = tmp_path / package / "1.2.3"
    import_marker = tmp_path / "staged-initializer-executed"
    stages = []
    def incomplete_install(command, **kwargs):
        stage = Path(command[command.index("--target") + 1])
        stages.append(stage)
        _write_distribution(stage, package, import_marker=import_marker)
        record = stage / f"{package}-1.2.3.dist-info" / "RECORD"
        rows = list(csv.reader(record.read_text().splitlines()))
        output = StringIO()
        csv.writer(output).writerows(row for row in rows if row[0] != package + "/__init__.py")
        record.write_text(output.getvalue())
    monkeypatch.setattr(tool_loader.subprocess, "run", incomplete_install)
    with pytest.raises(ValueError, match="import member"):
        ToolRegistry(store_path=tmp_path).install_package(package, "1.2.3")
    assert len(stages) == 1 and not stages[0].exists()
    assert not target.exists()
    assert not import_marker.exists()


@pytest.mark.parametrize("empty_winner", [True, False], ids=["empty-winner", "foreign-sentinel-winner"])
def test_atomic_publication_preserves_a_racing_target(tmp_path, monkeypatch, empty_winner):
    from bioimageflow.filesystem import publish_no_replace

    package = _package(tmp_path)
    target = tmp_path / package / "1.2.3"
    stages = []
    winner_identity = []
    def install(command, **kwargs):
        stage = Path(command[command.index("--target") + 1])
        stages.append(stage)
        _write_distribution(stage, package)
    def create_winner_then_publish(stage, destination):
        destination.mkdir()
        if not empty_winner:
            (destination / "foreign-sentinel").write_text("preserve")
        stat = destination.stat()
        winner_identity.append((stat.st_dev, stat.st_ino))
        # Only the race boundary is injected. The real OS exclusive rename
        # primitive runs against the newly occupied destination.
        publish_no_replace(stage, destination)
    monkeypatch.setattr(tool_loader.subprocess, "run", install)
    monkeypatch.setattr(tool_loader, "publish_no_replace", create_winner_then_publish)
    with pytest.raises(FileExistsError):
        ToolRegistry(store_path=tmp_path).install_package(package, "1.2.3")
    stat = target.stat()
    assert winner_identity == [(stat.st_dev, stat.st_ino)]
    assert len(stages) == 1 and not stages[0].exists()
    if empty_winner:
        assert list(target.iterdir()) == []
    else:
        assert sorted(path.name for path in target.iterdir()) == ["foreign-sentinel"]
        assert (target / "foreign-sentinel").read_text() == "preserve"
