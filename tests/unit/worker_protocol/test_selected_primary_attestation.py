"""Public selected-primary SDK admission and resident reuse controls."""

from dataclasses import replace
import base64
import csv
import hashlib
import importlib
import importlib.util
from io import StringIO
import json
import os
from pathlib import Path
import py_compile
import subprocess
import sys

import pytest

from bioimageflow import load_versioned_package
from bioimageflow.worker_origins import resolve_worker_tool_origin
from bioimageflow_core import (
    Arguments, ProcessingTask, RowInvocation, describe_tool_declaration,
    encode_processing_task, encode_worker_tool_origin,
)
from bioimageflow_core.primary_content import decode_primary_content, encode_primary_content
from bioimageflow_core.worker_origins import clear_worker_tool_instances, load_worker_tool


def _metadata(root, name):
    info = root / (name + "-1.0.0.dist-info")
    info.mkdir(exist_ok=True)
    (info / "METADATA").write_text("Metadata-Version: 2.1\nName: " + name.replace("_", "-") + "\nVersion: 1.0.0\n")
    (info / "top_level.txt").write_text(name + "\n")
    contents = StringIO()
    writer = csv.writer(contents)
    for item in sorted(root.rglob("*")):
        if item.is_file() and item.name != "RECORD" and "__pycache__" not in item.parts:
            data = item.read_bytes()
            digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).decode().rstrip("=")
            writer.writerow((item.relative_to(root).as_posix(), "sha256=" + digest, len(data)))
    writer.writerow(((info / "RECORD").relative_to(root).as_posix(), "", ""))
    (info / "RECORD").write_text(contents.getvalue())


@pytest.fixture(params=["source", "shared", "versioned", "installed"])
def selected(request, tmp_path, monkeypatch):
    kind = request.param
    name = "attested_primary_" + hashlib.sha256(str(tmp_path).encode()).hexdigest()[:12]
    constructed, called = tmp_path / "constructed", tmp_path / "called"
    source = f'''from pathlib import Path
from bioimageflow_core import ProcessingTool, IOModel, RowConsumption
class BaseTool(ProcessingTool):
    row_consumption = RowConsumption.MAPPED
    class Inputs(IOModel): pass
    class Outputs(IOModel): value: int
    def __init__(self):
        Path({str(constructed)!r}).touch()
    def process_row(self, arguments):
        Path({str(called)!r}).touch()
        return self.Outputs(value=1)
    def process_batch(self, arguments_list):
        return [self.process_row(arguments) for arguments in arguments_list]
'''
    if kind == "source":
        path = tmp_path / "single.py"
        path.write_text(source + "class WitnessTool(BaseTool):\n    row_consumption = BaseTool.row_consumption\n")
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, name, module)
        spec.loader.exec_module(module)
        root = tmp_path
    else:
        root = tmp_path / name / "1.0.0" if kind == "versioned" else tmp_path / "installation"
        package = root / name
        package.mkdir(parents=True)
        path = package / "base.py"
        path.write_text(source)
        (package / "__init__.py").write_text("from .tool import WitnessTool\n")
        (package / "tool.py").write_text("from .base import BaseTool\nclass WitnessTool(BaseTool):\n    row_consumption = BaseTool.row_consumption\n")
        _metadata(root, name)
        if kind == "versioned":
            module = load_versioned_package(name, "1.0.0", tmp_path)
        else:
            monkeypatch.syspath_prepend(str(root))
            module = importlib.import_module(name + ".tool")
    prefix = module.WitnessTool.__module__.split(".")[0]
    clear_worker_tool_instances()
    try:
        kwargs = {"installed_distribution": name.replace("_", "-")} if kind == "installed" else {}
        origin = resolve_worker_tool_origin(module.WitnessTool, **kwargs)
        yield type("Selected", (), dict(kind=kind, path=path, root=root, name=name, module=module,
            origin=origin, constructed=constructed, called=called))
    finally:
        clear_worker_tool_instances()
        for key in list(sys.modules):
            if key == prefix or key.startswith(prefix + "."):
                sys.modules.pop(key)


def test_unchanged_selected_primary_reuses_actual_instance_and_batch_owner(selected):
    first = load_worker_tool(selected.origin)
    assert selected.constructed.exists()
    assert first.process_row(Arguments()).value == 1
    assert [item.value for item in first.process_batch([Arguments()])] == [1]
    assert load_worker_tool(selected.origin) is first


def test_changed_selected_member_refuses_before_constructor_and_science(selected):
    imported = selected.root / "changed-member-imported"
    selected.path.write_text(f"from pathlib import Path\nPath({str(imported)!r}).touch()\n" + selected.path.read_text().replace("value=1", "value=9"))
    with pytest.raises(ImportError, match="[Pp]rimary.*hash|hash.*mismatch"):
        load_worker_tool(selected.origin)
    assert not selected.constructed.exists()
    assert not selected.called.exists()
    assert not imported.exists()
    assert sys.modules[selected.module.WitnessTool.__module__].WitnessTool is selected.module.WitnessTool


def test_new_b_digest_cannot_relabel_warmed_resident_a(selected):
    first = load_worker_tool(selected.origin)
    assert first.process_row(Arguments()).value == 1
    selected.constructed.unlink()
    selected.called.unlink()
    selected.path.write_text(selected.path.read_text().replace("value=1", "value=9"))
    members = tuple(replace(member, source_hash=hashlib.sha256(selected.path.read_bytes()).hexdigest())
        if member.module.endswith(".base") or member.module == selected.module.__name__ and selected.kind == "source" else member
        for member in selected.origin.primary.members)
    changed = replace(selected.origin, primary=replace(selected.origin.primary, members=members))
    if selected.kind == "source":
        changed = replace(changed, source_hash=hashlib.sha256(selected.path.read_bytes()).hexdigest())
        admitted_b = load_worker_tool(changed)
        assert admitted_b is not first and type(admitted_b) is not type(first)
        assert admitted_b.process_row(Arguments()).value == 9
        assert first.process_row(Arguments()).value == 1
        assert sys.modules[selected.module.__name__] is selected.module
        return
    with pytest.raises((ImportError, ValueError), match="Resident|resident|primary|Primary"):
        load_worker_tool(changed)
    assert not selected.constructed.exists()
    assert not selected.called.exists()
    assert first.process_row(Arguments()).value == 1


def test_warmed_instance_callback_replacement_refuses_before_science(selected):
    instance = load_worker_tool(selected.origin)
    def replacement(arguments):
        selected.called.touch()
        return instance.Outputs(value=9)
    instance.process_row = replacement
    with pytest.raises(ValueError, match="Resident/source|primary|Primary"):
        load_worker_tool(selected.origin)
    assert not selected.called.exists()


def test_primary_proof_decode_is_pure_and_requires_every_owner(selected, monkeypatch):
    payload = encode_primary_content(selected.origin.primary)
    def forbidden_read(*args, **kwargs):
        pytest.fail("Pure proof decoding read executable bytes")
    monkeypatch.setattr(Path, "read_bytes", forbidden_read)
    assert decode_primary_content(payload) == selected.origin.primary
    payload["callbacks"].pop()
    with pytest.raises(ValueError, match="callback|owner"):
        decode_primary_content(payload)


def test_fixed_size_mtime_stale_bytecode_never_supplies_admitted_primary(tmp_path, monkeypatch):
    name = "bytecode_primary_" + hashlib.sha256(str(tmp_path).encode()).hexdigest()[:12]
    source = tmp_path / (name + ".py")
    text = '''from bioimageflow_core import ProcessingTool, IOModel, RowConsumption
class WitnessTool(ProcessingTool):
    row_consumption = RowConsumption.MAPPED
    class Inputs(IOModel): pass
    class Outputs(IOModel): value: int
    def process_row(self, arguments): return self.Outputs(value=1)
'''
    source.write_text(text)
    before = source.stat()
    py_compile.compile(str(source), doraise=True)
    source.write_text(text.replace("value=1", "value=9"))
    os.utime(source, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert source.stat().st_size == before.st_size and source.stat().st_mtime_ns == before.st_mtime_ns
    # Capture the clean controller from its current source; existing A pyc stays
    # present and the SDK must independently import the held admitted B bytes.
    module = type(sys)(name)
    module.__file__ = str(source)
    monkeypatch.setitem(sys.modules, name, module)
    exec(compile(source.read_bytes(), str(source), "exec", dont_inherit=True), vars(module))
    origin = resolve_worker_tool_origin(module.WitnessTool)
    clear_worker_tool_instances()
    try:
        tool = load_worker_tool(origin)
        assert tool.process_row(Arguments()).value == 9
        assert load_worker_tool(origin) is tool
    finally:
        clear_worker_tool_instances()


def test_constructor_callback_mutation_refuses_before_method(tmp_path, monkeypatch):
    path = tmp_path / "constructor.py"
    called = tmp_path / "called"
    path.write_text(f'''from pathlib import Path
from bioimageflow_core import ProcessingTool, IOModel, RowConsumption
class WitnessTool(ProcessingTool):
    row_consumption = RowConsumption.MAPPED
    class Inputs(IOModel): pass
    class Outputs(IOModel): value: int
    def __init__(self): self.process_row = self.other
    def other(self, arguments):
        Path({str(called)!r}).touch()
        return self.Outputs(value=9)
    def process_row(self, arguments): return self.Outputs(value=1)
''')
    module = type(sys)("constructor_primary")
    module.__file__ = str(path)
    monkeypatch.setitem(sys.modules, module.__name__, module)
    exec(compile(path.read_bytes(), str(path), "exec", dont_inherit=True), vars(module))
    origin = resolve_worker_tool_origin(module.WitnessTool)
    clear_worker_tool_instances()
    try:
        with pytest.raises(ValueError, match="Resident/source|primary|Primary"):
            load_worker_tool(origin)
        assert not called.exists()
    finally:
        clear_worker_tool_instances()


@pytest.mark.parametrize("omitted", ["tool", "helper"])
def test_installed_primary_requires_complete_selected_package_before_import(tmp_path, monkeypatch, omitted):
    name = "complete_primary_" + hashlib.sha256(str(tmp_path).encode()).hexdigest()[:12]
    package = tmp_path / name
    package.mkdir()
    (package / "__init__.py").write_text("")
    (package / "helper.py").write_text("factor = 1\n")
    (package / "tool.py").write_text("from .base import Base\nfrom .helper import factor\nclass Tool(Base):\n    row_consumption = Base.row_consumption\n    factor = factor\n")
    (package / "base.py").write_text(f'''from pathlib import Path
from bioimageflow_core import ProcessingTool, IOModel, RowConsumption
class Base(ProcessingTool):
    row_consumption = RowConsumption.MAPPED
    class Inputs(IOModel): pass
    class Outputs(IOModel): value: int
    def __init__(self): Path({str(tmp_path / 'constructed')!r}).touch()
    def process_row(self, arguments):
        Path({str(tmp_path / 'called')!r}).touch()
        return self.Outputs(value=self.factor)
    def process_batch(self, arguments_list):
        return [self.process_row(arguments) for arguments in arguments_list]
''')
    _metadata(tmp_path, name)
    monkeypatch.syspath_prepend(str(tmp_path))
    module = importlib.import_module(name + ".tool")
    try:
        origin = resolve_worker_tool_origin(module.Tool, installed_distribution=name.replace("_", "-"))
    finally:
        for key in list(sys.modules):
            if key == name or key.startswith(name + "."):
                sys.modules.pop(key)
    child = '''import json, sys
from pathlib import Path
from bioimageflow_core import Arguments, decode_worker_tool_origin
from bioimageflow_core.worker_origins import load_worker_tool
root = Path(sys.argv[1]); sys.path.insert(0, str(root))
try:
    origin = decode_worker_tool_origin(json.loads((root / 'origin.json').read_text()))
    tool = load_worker_tool(origin)
    result = {'status': 'accepted', 'value': tool.process_row(Arguments()).value}
except (ImportError, ValueError) as error:
    result = {'status': 'refused', 'error': str(error)}
result.update({leaf: (root / leaf).exists() for leaf in ('imported', 'constructed', 'called')})
print(json.dumps(result))
'''

    def dispatch(proof_origin):
        (tmp_path / "origin.json").write_text(json.dumps(encode_worker_tool_origin(proof_origin)))
        result = subprocess.run([sys.executable, "-c", child, str(tmp_path)], capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout)

    assert dispatch(origin) == dict(status="accepted", value=1, imported=False, constructed=True, called=True)
    (tmp_path / "constructed").unlink()
    (tmp_path / "called").unlink()
    members = tuple(member for member in origin.primary.members if member.module != name + "." + omitted)
    assert len(members) == len(origin.primary.members) - 1
    incomplete = replace(origin, primary=replace(origin.primary, members=members))
    assert incomplete.primary.callbacks == origin.primary.callbacks and len(incomplete.primary.callbacks) == 4
    path = package / (omitted + ".py")
    path.write_text(f"from pathlib import Path\nPath({str(tmp_path / 'imported')!r}).touch()\n" + path.read_text().replace("factor = factor", "factor = 9").replace("factor = 1", "factor = 9"))
    _metadata(tmp_path, name)
    result = dispatch(incomplete)
    assert result["status"] == "refused", result
    assert "primary" in result["error"].lower()
    assert not any(result[leaf] for leaf in ("imported", "constructed", "called")), result


@pytest.mark.parametrize("mode", ["row_chunk", "process_batch"])
def test_processing_task_holds_admitted_lazy_helper_bytes_through_callback(tmp_path, monkeypatch, mode):
    name = "lazy_primary_" + hashlib.sha256(str(tmp_path).encode()).hexdigest()[:12]
    package = tmp_path / name
    package.mkdir()
    (package / "__init__.py").write_text("")
    helper = package / "helper.py"
    helper.write_text("value = 1\n")
    before = helper.stat()
    pyc = Path(py_compile.compile(str(helper), doraise=True))
    cached_a = pyc.read_bytes()
    helper.write_text("value = 9\n")
    os.utime(helper, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert helper.stat().st_size == before.st_size and helper.stat().st_mtime_ns == before.st_mtime_ns
    assert pyc.read_bytes() == cached_a
    (package / "tool.py").write_text('''from bioimageflow_core import ProcessingTool, IOModel, RowConsumption
class Tool(ProcessingTool):
    row_consumption = RowConsumption.MAPPED
    class Inputs(IOModel): pass
    class Outputs(IOModel): value: int
    def process_row(self, arguments):
        from .helper import value
        return self.Outputs(value=value)
    def process_batch(self, arguments_list):
        return [self.process_row(arguments) for arguments in arguments_list]
''')
    _metadata(tmp_path, name)
    monkeypatch.syspath_prepend(str(tmp_path))
    module = importlib.import_module(name + ".tool")
    try:
        origin = resolve_worker_tool_origin(module.Tool, installed_distribution=name.replace("_", "-"))
        assert name + ".helper" not in sys.modules
        run_dir = tmp_path / "run"
        batch_context = dict(run_dir=str(run_dir), assets_dir=str(run_dir / "assets"),
            work_dir=str(run_dir / "work"), rows_dir=str(run_dir / "work/rows"),
            row_dir=None, batch_dir=str(run_dir / "work/batch"), row_index=None)
        invocation = ProcessingTask(
            task_id="task_0000000000000000", node_name="lazy_helper",
            invocation_id="inv_" + "1" * 32, cache_attempt_id=None,
            task_retry=0, mode=mode, row_consumption="mapped", tool=origin,
            declaration=describe_tool_declaration(module.Tool),
            rows=(RowInvocation(0, "sample", {}, None),),
            batch_context=batch_context if mode == "process_batch" else None,
        )
    finally:
        for key in list(sys.modules):
            if key == name or key.startswith(name + "."):
                sys.modules.pop(key)
    assert any(member.module == name + ".helper" and member.source_hash == hashlib.sha256(helper.read_bytes()).hexdigest()
        for member in origin.primary.members)
    (tmp_path / "task.json").write_text(json.dumps(encode_processing_task(invocation)))
    child = '''import json, sys
from pathlib import Path
from bioimageflow_core import decode_processing_result
from bioimageflow_core.worker import execute_processing_task
root = Path(sys.argv[1]); sys.path.insert(0, str(root))
payload = json.loads((root / 'task.json').read_text())
assert sys.argv[2] + '.helper' not in sys.modules
results = [decode_processing_result(execute_processing_task(payload)) for _ in range(2)]
print(json.dumps([[output['value'] for group in result.groups for output in group.outputs] for result in results]))
'''
    result = subprocess.run([sys.executable, "-c", child, str(tmp_path), name], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == [[9], [9]]
