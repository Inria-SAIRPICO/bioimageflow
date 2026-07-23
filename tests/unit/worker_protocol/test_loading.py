"""Strict origin loading and origin-aware instance tests."""

from __future__ import annotations

import hashlib
import importlib
import sys

import pytest
from bioimageflow_core import (
    ArchiveModuleOriginV1,
    InstalledModuleOriginV1,
    SharedModuleOriginV1,
    SourceFileOriginV1,
    VersionedModuleOriginV1,
)
from bioimageflow_core.worker_origins import (
    clear_worker_tool_instances,
    load_worker_tool,
)


TOOL_SOURCE = """
from bioimageflow_core import Arguments, IOModel, ProcessingTool

class SameNameTool(ProcessingTool):
    class Inputs(IOModel):
        value: str
    class Outputs(IOModel):
        value: str
    def __init__(self):
        self.calls = 0
    def process_row(self, arguments: Arguments):
        self.calls += 1
        return self.Outputs(value=arguments.value)
"""


@pytest.fixture(autouse=True)
def _clear_instances():
    clear_worker_tool_instances()
    yield
    clear_worker_tool_instances()


def _write_source(path) -> str:
    path.write_text(TOOL_SOURCE, encoding="utf-8")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_source_file_hash_mismatch_fails(tmp_path) -> None:
    source = tmp_path / "tool.py"
    marker = tmp_path / "executed"
    source.write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('executed')\n",
        encoding="utf-8",
    )
    origin = SourceFileOriginV1(
        path=str(source.resolve()),
        source_hash="0" * 64,
        class_name="SameNameTool",
    )
    with pytest.raises(ImportError, match="hash mismatch"):
        load_worker_tool(origin)
    assert not marker.exists()


def test_complete_origin_separates_equal_class_names(tmp_path) -> None:
    source_a = tmp_path / "a.py"
    source_b = tmp_path / "b.py"
    hash_a = _write_source(source_a)
    hash_b = _write_source(source_b)
    first_origin = SourceFileOriginV1(
        path=str(source_a.resolve()),
        source_hash=hash_a,
        class_name="SameNameTool",
    )
    second_origin = SourceFileOriginV1(
        path=str(source_b.resolve()),
        source_hash=hash_b,
        class_name="SameNameTool",
    )
    first = load_worker_tool(first_origin)
    assert load_worker_tool(first_origin) is first
    assert load_worker_tool(second_origin) is not first


def test_equal_shared_module_names_from_different_roots_are_isolated(tmp_path) -> None:
    origins = []
    for directory in ("one", "two"):
        root = tmp_path / directory
        package = root / "same_tools"
        package.mkdir(parents=True)
        (package / "__init__.py").write_text("", encoding="utf-8")
        source = package / "worker.py"
        source_hash = _write_source(source)
        origins.append(
            SharedModuleOriginV1(
                module="same_tools.worker",
                import_root=str(root.resolve()),
                source_hash=source_hash,
                class_name="SameNameTool",
            )
        )
    first = load_worker_tool(origins[0])
    second = load_worker_tool(origins[1])
    assert first is not second
    assert "same_tools.worker" not in sys.modules


def test_shared_module_import_escape_fails(tmp_path, monkeypatch) -> None:
    actual_root = tmp_path / "actual"
    package = actual_root / "escaped_tools"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    source = package / "worker.py"
    source_hash = _write_source(source)
    declared_root = tmp_path / "declared"
    declared_root.mkdir()
    monkeypatch.syspath_prepend(str(actual_root))
    origin = SharedModuleOriginV1(
        module="escaped_tools.worker",
        import_root=str(declared_root.resolve()),
        source_hash=source_hash,
        class_name="SameNameTool",
    )
    with pytest.raises(ImportError, match="absent from"):
        load_worker_tool(origin)


def _write_distribution_metadata(root, name: str, version: str, package: str) -> None:
    metadata = root / f"{package}-{version}.dist-info"
    metadata.mkdir()
    (metadata / "METADATA").write_text(
        f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n",
        encoding="utf-8",
    )
    (metadata / "top_level.txt").write_text(f"{package}\n", encoding="utf-8")


def test_two_versioned_origins_load_separate_instances(tmp_path) -> None:
    origins = []
    for version in ("1.0.0", "2.0.0"):
        root = tmp_path / version
        package = root / "versioned_tools"
        package.mkdir(parents=True)
        (package / "__init__.py").write_text("", encoding="utf-8")
        (package / "worker.py").write_text(TOOL_SOURCE, encoding="utf-8")
        _write_distribution_metadata(
            root, "versioned-tools", version, "versioned_tools"
        )
        scoped = f"versioned_tools__{version.replace('.', '_')}"
        origins.append(
            VersionedModuleOriginV1(
                distribution="versioned-tools",
                import_package="versioned_tools",
                version=version,
                canonical_module="versioned_tools.worker",
                scoped_module=f"{scoped}.worker",
                store_root=str(root.resolve()),
                class_name="SameNameTool",
            )
        )
    assert load_worker_tool(origins[0]) is not load_worker_tool(origins[1])


def test_installed_distribution_version_mismatch_fails() -> None:
    try:
        actual = importlib.metadata.version("bioimageflow-core")
    except importlib.metadata.PackageNotFoundError:
        pytest.skip("bioimageflow-core metadata is unavailable")
    origin = InstalledModuleOriginV1(
        distribution="bioimageflow-core",
        version=f"{actual}.mismatch",
        module="bioimageflow_core.worker",
        class_name="ProcessingTool",
    )
    with pytest.raises(ImportError, match="version mismatch"):
        load_worker_tool(origin)


def _archive_hash(package_root) -> str:
    digest = hashlib.sha256()
    for path in sorted(package_root.rglob("*.py")):
        if path.name == "__init__.py" and path.stat().st_size == 0:
            continue
        relative = path.relative_to(package_root).as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).hexdigest().encode("ascii"))
        digest.update(b"\0")
    return digest.hexdigest()


def test_two_archive_origins_load_separate_instances(tmp_path) -> None:
    origins = []
    for source_id in ("m_1111111111111111", "m_2222222222222222"):
        root = tmp_path / source_id
        package_name = f"archive_{source_id}"
        package = root / package_name
        package.mkdir(parents=True)
        (package / "__init__.py").write_text("", encoding="utf-8")
        (package / "worker.py").write_text(TOOL_SOURCE, encoding="utf-8")
        origins.append(
            ArchiveModuleOriginV1(
                source_id=source_id,
                source_hash=_archive_hash(package),
                canonical_module="tools.worker",
                scoped_module=f"{package_name}.worker",
                materialization_root=str(root.resolve()),
                class_name="SameNameTool",
            )
        )
    assert load_worker_tool(origins[0]) is not load_worker_tool(origins[1])
