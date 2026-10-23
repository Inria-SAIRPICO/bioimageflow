"""Held installed members and fresh editable source capture stay separate."""

import importlib.util
import sys

import pytest

from bioimageflow_core.primary_content import capture_primary_content, capture_primary_package, primary_import_context


@pytest.fixture
def ordinary_package(tmp_path, monkeypatch):
    package = tmp_path / 'captured_primary'
    package.mkdir()
    source = package / '__init__.py'
    source.write_text('''from bioimageflow_core import IOModel, ProcessingTool, RowConsumption
class Tool(ProcessingTool):
    row_consumption = RowConsumption.MAPPED
    class Inputs(IOModel): pass
    class Outputs(IOModel): value: int
    def process_row(self, arguments): return self.Outputs(value=1)
''')
    spec = importlib.util.spec_from_file_location('captured_primary', source)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, module.__name__, module)
    # Load ordinary, unrewritten source so pytest cannot become code authority.
    exec(compile(source.read_bytes(), str(source), 'exec', dont_inherit=True), vars(module))
    return package, source, module


def test_held_package_capture_reuses_members_without_new_reads_or_walks(ordinary_package, monkeypatch):
    from pathlib import Path

    package, source, module = ordinary_package
    held = capture_primary_package(module.__name__, package)
    original = capture_primary_content(module.Tool, package_root=package)
    reads, walks = [], []
    read_bytes, rglob = Path.read_bytes, Path.rglob

    def read(path):
        reads.append(path)
        return read_bytes(path)

    def walk(path, pattern):
        walks.append(path)
        return rglob(path, pattern)

    monkeypatch.setattr(Path, 'read_bytes', read)
    monkeypatch.setattr(Path, 'rglob', walk)
    first = capture_primary_content(module.Tool, package_root=package, package_admission=held)
    second = capture_primary_content(module.Tool, package_root=package, package_admission=held)
    assert first.proof == second.proof == original.proof
    assert first.scientific_facts() == original.scientific_facts()
    assert first.sources[module.__name__] is held.sources[module.__name__]
    assert source not in reads and walks == []
    # This source checkout installs Core editably; its inherited callback owners
    # deliberately remain fresh rather than borrowing installed-generation rules.
    # The admitted class remains bound to the held generation until retirement.
    source.write_text(source.read_text().replace('value=1', 'value=9'))
    assert capture_primary_content(module.Tool, package_root=package, package_admission=held).proof == original.proof


def test_editable_capture_rechecks_current_bytes_and_refuses_stale_class(ordinary_package):
    package, source, module = ordinary_package
    before = capture_primary_content(module.Tool, package_root=package)
    source.write_text(source.read_text().replace('value=1', 'value=9'))
    with pytest.raises(ValueError, match='Resident/source callable mismatch'):
        capture_primary_content(module.Tool, package_root=package)
    # Recompiling fresh bytes produces a new supported editable source generation.
    exec(compile(source.read_bytes(), str(source), 'exec', dont_inherit=True), vars(module))
    after = capture_primary_content(module.Tool, package_root=package)
    assert after.scientific_facts() != before.scientific_facts()


def test_lazy_namespace_members_are_owned_and_new_disk_members_refuse(tmp_path, monkeypatch):
    package = tmp_path / 'held_namespace'
    namespace = package / 'nested'
    namespace.mkdir(parents=True)
    (package / '__init__.py').write_text('')
    (namespace / 'helper.py').write_text('value = 1\n')
    held = capture_primary_package('held_namespace', package)
    assert held.package_roots == {'held_namespace': package}
    (namespace / 'added.py').write_text('value = 9\n')
    before = set(sys.modules)
    try:
        with primary_import_context(held):
            import held_namespace.nested.helper
            assert held_namespace.nested.helper.value == 1
            assert held.owned_modules['held_namespace.nested'] is sys.modules['held_namespace.nested']
            with pytest.raises(ImportError, match='no held source member'):
                __import__('held_namespace.nested.added')
    finally:
        for name in set(sys.modules) - before:
            if name == 'held_namespace' or name.startswith('held_namespace.'):
                sys.modules.pop(name)
