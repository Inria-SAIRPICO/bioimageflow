"""Strict origin loading and origin-aware instance tests."""

from __future__ import annotations

import hashlib
import importlib
from pathlib import Path
from types import ModuleType
import sys

import pytest
from bioimageflow_core import (
    ArchiveModuleOrigin,
    InstalledModuleOrigin,
    SharedModuleOrigin,
    SourceFileOrigin,
    VersionedModuleOrigin,
)
from bioimageflow_core.worker_origins import (
    clear_worker_tool_instances,
    load_worker_tool,
    worker_tool_origin_identity,
)
from bioimageflow_core import ProcessingTool
from bioimageflow_core.primary_content import capture_primary_content
from tests.testkit.primary_content import source_proof


TOOL_SOURCE = """
from bioimageflow_core import Arguments, IOModel, ProcessingTool, RowConsumption

class SameNameTool(ProcessingTool):
    row_consumption = RowConsumption.MAPPED
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
def _clear_instances(tmp_path):
    previous = dict(sys.modules)
    clear_worker_tool_instances()
    yield
    clear_worker_tool_instances()
    for name, module in list(sys.modules.items()):
        source = getattr(module, "__file__", None)
        if name not in previous and isinstance(source, str) and Path(source).is_relative_to(tmp_path):
            sys.modules.pop(name)


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
    origin = SourceFileOrigin(
        path=str(source.resolve()),
        source_hash="0" * 64,
        class_name="SameNameTool",
        primary=source_proof(source, "SameNameTool"),
    )
    with pytest.raises(ImportError, match="hash mismatch"):
        load_worker_tool(origin)
    assert not marker.exists()


def test_complete_origin_separates_equal_class_names(tmp_path) -> None:
    source_a = tmp_path / "a.py"
    source_b = tmp_path / "b.py"
    hash_a = _write_source(source_a)
    hash_b = _write_source(source_b)
    first_origin = SourceFileOrigin(
        path=str(source_a.resolve()),
        source_hash=hash_a,
        class_name="SameNameTool",
        primary=source_proof(source_a, "SameNameTool"),
    )
    second_origin = SourceFileOrigin(
        path=str(source_b.resolve()),
        source_hash=hash_b,
        class_name="SameNameTool",
        primary=source_proof(source_b, "SameNameTool"),
    )
    first = load_worker_tool(first_origin)
    assert load_worker_tool(first_origin) is first
    assert load_worker_tool(second_origin) is not first


def test_shared_conflicting_root_refuses_without_replacing_admitted_owner(tmp_path) -> None:
    origins = []
    for directory in ("one", "two"):
        root = tmp_path / directory
        package = root / "same_tools"
        package.mkdir(parents=True)
        (package / "__init__.py").write_text("", encoding="utf-8")
        source = package / "worker.py"
        constructor = root / "constructed"
        source.write_text(TOOL_SOURCE.replace("self.calls = 0", f"self.calls = 0; __import__('pathlib').Path({str(constructor)!r}).touch()"))
        source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
        origins.append(
            SharedModuleOrigin(
                module="same_tools.worker",
                import_root=str(root.resolve()),
                source_hash=source_hash,
                class_name="SameNameTool",
                primary=source_proof(source, "SameNameTool", module="same_tools.worker", package_root=package),
            )
        )
    first = load_worker_tool(origins[0])
    namespace = {name: module for name, module in sys.modules.items() if name == "same_tools" or name.startswith("same_tools.")}
    assert (tmp_path / "one" / "constructed").exists()
    with pytest.raises(ImportError, match="[Rr]esident.*[Pp]rimary|conflicts"):
        load_worker_tool(origins[1])
    assert not (tmp_path / "two" / "constructed").exists()
    assert all(sys.modules[name] is module for name, module in namespace.items())
    assert load_worker_tool(origins[0]) is first
    assert first.calls == 0


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
    origin = SharedModuleOrigin(
        module="escaped_tools.worker",
        import_root=str(declared_root.resolve()),
        source_hash=source_hash,
        class_name="SameNameTool",
        primary=source_proof(source, "SameNameTool", module="escaped_tools.worker", package_root=package),
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
            VersionedModuleOrigin(
                distribution="versioned-tools",
                import_package="versioned_tools",
                version=version,
                canonical_module="versioned_tools.worker",
                scoped_module=f"{scoped}.worker",
                store_root=str(root.resolve()),
                class_name="SameNameTool",
                primary=source_proof(package / "worker.py", "SameNameTool", module=scoped + ".worker", package_root=package),
            )
        )
    assert load_worker_tool(origins[0]) is not load_worker_tool(origins[1])


def test_installed_distribution_version_mismatch_fails() -> None:
    try:
        actual = importlib.metadata.version("bioimageflow-core")
    except importlib.metadata.PackageNotFoundError:
        pytest.skip("bioimageflow-core metadata is unavailable")
    origin = InstalledModuleOrigin(
        distribution="bioimageflow-core",
        version=f"{actual}.mismatch",
        module="bioimageflow_core.worker",
        class_name="ProcessingTool",
        primary=capture_primary_content(ProcessingTool).proof,
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
            ArchiveModuleOrigin(
                source_id=source_id,
                source_hash=_archive_hash(package),
                canonical_module="tools.worker",
                scoped_module=f"{package_name}.worker",
                materialization_root=str(root.resolve()),
                class_name="SameNameTool",
                primary=source_proof(package / "worker.py", "SameNameTool", module=package_name + ".worker", package_root=package),
            )
        )
    assert load_worker_tool(origins[0]) is not load_worker_tool(origins[1])


def test_source_file_executes_the_bytes_that_were_hashed(tmp_path, monkeypatch) -> None:
    source = tmp_path / "admitted.py"
    admitted = TOOL_SOURCE + "\nSameNameTool.admitted_value = 1\n"
    source.write_text(admitted)
    source_hash = hashlib.sha256(admitted.encode()).hexdigest()
    primary = source_proof(source, "SameNameTool")
    original_read = Path.read_bytes

    def mutate_after_read(path):
        contents = original_read(path)
        if path == source:
            source.write_text(
                admitted.replace("admitted_value = 1", "admitted_value = 9")
            )
        return contents

    monkeypatch.setattr(Path, "read_bytes", mutate_after_read)
    tool = load_worker_tool(
        SourceFileOrigin(str(source), source_hash, "SameNameTool", primary)
    )
    assert tool.admitted_value == 1


def test_source_file_refuses_foreign_reexport_before_construction(
    tmp_path, monkeypatch
) -> None:
    foreign = tmp_path / "foreign.py"
    marker = tmp_path / "constructed"
    foreign.write_text(
        TOOL_SOURCE.replace(
            "self.calls = 0",
            f"self.calls = 0; __import__('pathlib').Path({str(marker)!r}).touch()",
        )
    )
    spec = importlib.util.spec_from_file_location("origin_foreign_tool", foreign)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    source = tmp_path / "selected.py"
    source.write_text("from origin_foreign_tool import SameNameTool\n")
    origin = SourceFileOrigin(
        str(source), hashlib.sha256(source.read_bytes()).hexdigest(), "SameNameTool", source_proof(source, "SameNameTool")
    )
    with pytest.raises(ImportError, match="defining module"):
        load_worker_tool(origin)
    assert not marker.exists()
    assert sys.modules[spec.name] is module


def test_versioned_cached_root_must_match_selected_store(tmp_path, monkeypatch) -> None:
    root = tmp_path / "selected"
    package = root / "checked_tools"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text(TOOL_SOURCE)
    _write_distribution_metadata(root, "checked-tools", "1.0", "checked_tools")
    foreign = tmp_path / "foreign.py"
    foreign.write_text(TOOL_SOURCE)
    module = ModuleType("checked_tools__1_0")
    module.__file__ = str(foreign)
    exec(compile(TOOL_SOURCE, str(foreign), "exec"), module.__dict__)
    monkeypatch.setitem(sys.modules, module.__name__, module)
    child = ModuleType(module.__name__ + ".worker")
    child.__file__ = str(package / "worker.py")
    (package / "worker.py").write_text(TOOL_SOURCE)
    exec(compile(TOOL_SOURCE, child.__file__, "exec"), child.__dict__)
    monkeypatch.setitem(sys.modules, child.__name__, child)
    origin = VersionedModuleOrigin(
        "checked-tools",
        "checked_tools",
        "1.0",
        "checked_tools.worker",
        child.__name__,
        str(root),
        "SameNameTool",
        source_proof(package / "worker.py", "SameNameTool", module=child.__name__, package_root=package),
    )
    with pytest.raises(ImportError, match="root|store"):
        load_worker_tool(origin)
    assert sys.modules[module.__name__] is module


def test_versioned_failed_initialization_can_retry(tmp_path, monkeypatch) -> None:
    root = tmp_path / "selected"
    package = root / "retry_tools"
    package.mkdir(parents=True)
    marker = tmp_path / "attempted"
    (package / "__init__.py").write_text(
        f"from pathlib import Path\nmarker = Path({str(marker)!r})\n"
        "if not marker.exists():\n    marker.touch()\n    raise RuntimeError('first attempt')\n"
        + TOOL_SOURCE
    )
    _write_distribution_metadata(root, "retry-tools", "1.0", "retry_tools")
    name = "retry_tools__1_0"
    origin = VersionedModuleOrigin(
        "retry-tools",
        "retry_tools",
        "1.0",
        "retry_tools",
        name,
        str(root),
        "SameNameTool",
        source_proof(package / "__init__.py", "SameNameTool", module=name, package_root=package),
    )
    try:
        with pytest.raises(RuntimeError, match="first attempt"):
            load_worker_tool(origin)
        tool = load_worker_tool(origin)
        assert (
            tool.process_row(
                __import__("bioimageflow_core").Arguments(value="retained")
            ).value
            == "retained"
        )
        assert load_worker_tool(origin) is tool
    finally:
        sys.modules.pop(name, None)


@pytest.mark.parametrize("kind", ["shared", "archive"])
def test_standalone_module_origin_preserves_valid_tool(tmp_path, kind):
    source = tmp_path / "standalone.py"
    digest = _write_source(source)
    origin = (
        SharedModuleOrigin("standalone", str(tmp_path), digest, "SameNameTool", source_proof(source, "SameNameTool", module="standalone"))
        if kind == "shared"
        else ArchiveModuleOrigin(
            "standalone",
            digest,
            "standalone",
            "standalone",
            str(tmp_path),
            "SameNameTool",
            source_proof(source, "SameNameTool", module="standalone"),
        )
    )
    tool = load_worker_tool(origin)
    from bioimageflow_core import Arguments

    assert tool.process_row(Arguments(value="standalone")).value == "standalone"


def test_source_namespace_preserves_preexisting_module_and_allows_clean_retry(
    tmp_path, monkeypatch
):
    source = tmp_path / "selected.py"
    marker = tmp_path / "executed"
    source.write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).touch()\n" + TOOL_SOURCE
    )
    origin = SourceFileOrigin(
        str(source), hashlib.sha256(source.read_bytes()).hexdigest(), "SameNameTool", source_proof(source, "SameNameTool")
    )
    name = "_bioimageflow_worker_" + worker_tool_origin_identity(origin)
    sentinel = ModuleType(name)
    sentinel.__file__ = str(source)
    exec(
        compile(
            TOOL_SOURCE + "\nSameNameTool.admitted_value = 99\n", str(source), "exec"
        ),
        sentinel.__dict__,
    )
    monkeypatch.setitem(sys.modules, name, sentinel)
    with pytest.raises(ImportError, match="not admitted"):
        load_worker_tool(origin)
    assert sys.modules[name] is sentinel
    assert not marker.exists()
    monkeypatch.delitem(sys.modules, name)
    tool = load_worker_tool(origin)
    assert marker.exists()
    assert load_worker_tool(origin) is tool


def test_loader_owned_source_module_supports_instance_cache_reset(tmp_path):
    source = tmp_path / "owned.py"
    origin = SourceFileOrigin(str(source), _write_source(source), "SameNameTool", source_proof(source, "SameNameTool"))
    first = load_worker_tool(origin)
    clear_worker_tool_instances()
    second = load_worker_tool(origin)
    assert second is not first
    assert type(second) is type(first)
    assert load_worker_tool(origin) is second
