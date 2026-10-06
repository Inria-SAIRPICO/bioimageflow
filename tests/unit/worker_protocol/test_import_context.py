"""Actual dependency values, executor authority, and caller namespace preservation."""
import base64
import csv
import hashlib
import importlib.util
from importlib.metadata import version
from io import StringIO
import sys
from types import ModuleType

import pytest
from bioimageflow_core import Arguments, VersionedModuleOrigin
from bioimageflow_core.import_context import admit_import_root, selected_import_root
from bioimageflow_core.worker_origins import clear_worker_tool_instances, load_worker_tool
from tests.testkit.primary_content import source_proof


def _distribution(root, name, version, sources, requirements=()):
    for relative, text in sources.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    info = root / f'{name}-{version}.dist-info'
    info.mkdir(parents=True)
    (info / 'METADATA').write_text(f'Metadata-Version: 2.1\nName: {name.replace("_", "-")}\nVersion: {version}\n'
        + ''.join(f'Requires-Dist: {requirement}\n' for requirement in requirements))
    (info / 'top_level.txt').write_text(name + '\n')
    record = StringIO()
    writer = csv.writer(record)
    for path in [*(root / relative for relative in sources), info / 'METADATA', info / 'top_level.txt']:
        data = path.read_bytes()
        digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).decode().rstrip('=')
        writer.writerow((path.relative_to(root).as_posix(), 'sha256=' + digest, len(data)))
    writer.writerow(((info / 'RECORD').relative_to(root).as_posix(), '', ''))
    (info / 'RECORD').write_text(record.getvalue())


def _selected(root, *, requirement='witness-dependency==9.0.0', initializer=True, package='witness_tools', dependency_requirements=()):
    init = 'from witness_dependency import VALUE\n' if initializer else ''
    _distribution(root, package, '1.0.0', {
        package + '/__init__.py': init,
        package + '/worker.py': '''from bioimageflow_core import IOModel, ProcessingTool, RowConsumption
class WitnessTool(ProcessingTool):
    row_consumption=RowConsumption.MAPPED
    class Inputs(IOModel): pass
    class Outputs(IOModel): value:int
    def process_row(self, arguments):
        import witness_dependency
        return self.Outputs(value=witness_dependency.VALUE)
''',
    }, (requirement,))
    _distribution(root, 'witness_dependency', '9.0.0', {'witness_dependency.py': 'VALUE=9\n'}, dependency_requirements)
    return VersionedModuleOrigin(package.replace('_','-'), package, '1.0.0',
        package + '.worker', package + '__1_0_0.worker', str(root), 'WitnessTool',
        source_proof(root / package / 'worker.py', 'WitnessTool', module=package + '__1_0_0.worker', package_root=root / package))


@pytest.fixture(autouse=True)
def _owned_modules():
    names = ('witness_tools', 'witness_tools__1_0_0', 'witness_two', 'witness_two__1_0_0', 'witness_dependency')
    before = {name: module for name, module in sys.modules.items()
              if any(name == prefix or name.startswith(prefix + '.') for prefix in names)}
    before_path = list(sys.path)
    clear_worker_tool_instances()
    yield
    clear_worker_tool_instances()
    for name in list(sys.modules):
        if name not in before and any(name == prefix or name.startswith(prefix + '.') for prefix in names):
            sys.modules.pop(name)
    sys.modules.update(before)
    sys.path[:] = before_path


def test_selected_initializer_constructor_and_lazy_call_consume_nine(tmp_path):
    origin = _selected(tmp_path)
    before = list(sys.path)
    tool = load_worker_tool(origin)
    assert sys.path == before
    assert sys.modules['witness_tools__1_0_0'].VALUE == 9
    admission = admit_import_root(tmp_path, import_package='witness_tools')
    with selected_import_root(admission):
        assert tool.process_row(Arguments()).value == 9
    assert sys.path == before
    assert load_worker_tool(origin) is tool


def test_unknown_foreign_dependency_refuses_before_initializer(tmp_path, monkeypatch):
    origin = _selected(tmp_path)
    foreign = ModuleType('witness_dependency')
    foreign.VALUE = 1
    monkeypatch.setitem(sys.modules, 'witness_dependency', foreign)
    before = list(sys.path)
    with pytest.raises(ImportError, match='unknown loaded distribution ownership'):
        load_worker_tool(origin)
    assert sys.modules['witness_dependency'] is foreign
    assert 'witness_tools__1_0_0' not in sys.modules
    assert sys.path == before


def _foreign(root, monkeypatch):
    _distribution(root, 'witness_dependency', '1.0.0', {'witness_dependency.py': 'VALUE=1\n'})
    monkeypatch.syspath_prepend(str(root))
    spec = importlib.util.spec_from_file_location('witness_dependency', root / 'witness_dependency.py')
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, 'witness_dependency', module)
    spec.loader.exec_module(module)
    return module


def test_known_foreign_one_refuses_selected_nine_preserving_caller(tmp_path, monkeypatch):
    foreign = _foreign(tmp_path / 'foreign', monkeypatch)
    selected = tmp_path / 'selected'
    origin = _selected(selected)
    before = list(sys.path)
    with pytest.raises(ImportError, match='conflicts with selected installation'):
        load_worker_tool(origin)
    assert foreign.VALUE == 1 and sys.modules['witness_dependency'] is foreign
    assert sys.path == before
    assert 'witness_tools__1_0_0' not in sys.modules


def test_managed_runtime_uses_its_compatible_one_not_controller_copy_nine(tmp_path, monkeypatch):
    foreign = _foreign(tmp_path / 'runtime', monkeypatch)
    selected = tmp_path / 'selected'
    origin = _selected(selected, requirement='witness-dependency>=1.0.0',
                       dependency_requirements=('witness-dependency>=9.0.0',))
    before = list(sys.path)
    admission = admit_import_root(selected, import_package='witness_tools', dependency_authority='managed_runtime')
    with selected_import_root(admission):
        tool = load_worker_tool(origin, dependency_authority='managed_runtime')
        assert tool.process_row(Arguments()).value == 1
        assert str(selected) not in sys.path
    assert sys.modules['witness_dependency'] is foreign
    assert sys.path == before
    observed = {item['distribution']: item for item in admission.observed_dependencies}
    assert observed['witness-dependency'] == {'distribution': 'witness-dependency', 'version': '1.0.0', 'authority': 'managed_runtime'}
    assert observed['numpy']['version'] == version('numpy')
    assert observed['packaging']['version'] == version('packaging')


def test_managed_runtime_incompatible_active_requirement_refuses(tmp_path, monkeypatch):
    foreign = _foreign(tmp_path / 'runtime', monkeypatch)
    origin = _selected(tmp_path / 'selected')
    before = list(sys.path)
    with pytest.raises(ImportError, match='does not satisfy'):
        load_worker_tool(origin, dependency_authority='managed_runtime')
    assert sys.modules['witness_dependency'] is foreign
    assert 'witness_tools__1_0_0' not in sys.modules
    assert sys.path == before


def test_context_preserves_primary_error_and_caller_paths(tmp_path, monkeypatch):
    _selected(tmp_path)
    admission = admit_import_root(tmp_path, import_package='witness_tools')
    before = list(sys.path)
    foreign = ModuleType('witness_dependency')
    with pytest.raises(RuntimeError, match='primary tool error'):
        with selected_import_root(admission):
            monkeypatch.setitem(sys.modules, 'witness_dependency', foreign)
            raise RuntimeError('primary tool error')
    assert sys.path == before
    assert sys.modules['witness_dependency'] is foreign


def test_successive_tool_roots_reuse_verified_dependency_after_path_restore(tmp_path):
    first = _selected(tmp_path / 'one')
    second = _selected(tmp_path / 'two', package='witness_two')
    before = list(sys.path)
    first_tool = load_worker_tool(first)
    retained_dependency = sys.modules['witness_dependency']
    assert sys.path == before
    second_tool = load_worker_tool(second)
    assert sys.modules['witness_dependency'] is retained_dependency
    assert first_tool.process_row(Arguments()).value == second_tool.process_row(Arguments()).value == 9
    assert sys.path == before


def test_host_provided_sdk_requirement_is_checked_without_installed_copy(tmp_path):
    _selected(tmp_path, requirement='bioimageflow-core>=99.0.0')
    before = list(sys.path)
    with pytest.raises(ImportError, match='does not satisfy bioimageflow-core>=99.0.0'):
        admit_import_root(tmp_path, import_package='witness_tools')
    assert 'witness_tools__1_0_0' not in sys.modules
    assert sys.path == before


def test_admission_metadata_reads_stay_bounded_while_live_membership_revalidates(tmp_path, monkeypatch):
    import importlib.metadata as metadata

    _selected(tmp_path)
    reads = []
    original = metadata.PathDistribution.read_text
    def read(distribution, filename):
        reads.append(filename)
        return original(distribution, filename)
    monkeypatch.setattr(metadata.PathDistribution, 'read_text', read)
    admission = admit_import_root(tmp_path, import_package='witness_tools')
    with selected_import_root(admission):
        import witness_dependency
        assert witness_dependency.VALUE == 9
    warmed = len(reads)
    before = list(sys.path)
    for _ in range(20):
        with selected_import_root(admission):
            assert witness_dependency.VALUE == 9
    assert len(reads) == warmed
    assert sys.path == before
    assert admission.to_scientific_facts() == {'dependency_versions': {'witness-dependency': '9.0.0'}}
    foreign = ModuleType('witness_dependency')
    monkeypatch.setitem(sys.modules, 'witness_dependency', foreign)
    with pytest.raises(ImportError, match='unknown loaded distribution ownership'):
        with selected_import_root(admission):
            pytest.fail('Live foreign replacement reached the tool callback')
    assert sys.modules['witness_dependency'] is foreign and sys.path == before
    monkeypatch.setitem(sys.modules, 'witness_dependency', witness_dependency)
    dependency_metadata = tmp_path / 'witness_dependency-9.0.0.dist-info' / 'METADATA'
    dependency_metadata.write_text(dependency_metadata.read_text().replace('Version: 9.0.0', 'Version: 10.0.0'))
    with pytest.raises(ImportError, match='loaded version 10.0.0 does not satisfy witness-dependency==9.0.0'):
        admit_import_root(tmp_path, import_package='witness_tools')
    assert len(reads) > warmed
