"""Ordinary public loading admits inherited primary bytes before cache lookup.

Each clean controller is a new process. The managed provider is the existing
public fake seam; canonical SDK tasks execute, but no Pixi/OS claim is made.
"""

import base64
import csv
import hashlib
import importlib
import importlib.metadata
import importlib.util
from io import StringIO
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from bioimageflow import Workflow, WorkflowExecutionContext, load_versioned_package
from bioimageflow import env_manager
from bioimageflow.storage import Storage
from wetlands import EnvironmentSpec

from .test_environment_content_identity import _Environment, _Provider


def _write_metadata(root):
    info = root / "selected_primary_tools-1.0.0.dist-info"
    info.mkdir(exist_ok=True)
    (info / "METADATA").write_text("Metadata-Version: 2.1\nName: selected-primary-tools\nVersion: 1.0.0\n")
    (info / "top_level.txt").write_text("selected_primary_tools\n")
    record = StringIO()
    writer = csv.writer(record)
    for path in sorted(root.rglob("*")):
        if path.is_file() and path.name != "RECORD" and "__pycache__" not in path.parts:
            data = path.read_bytes()
            digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).decode().rstrip("=")
            writer.writerow((path.relative_to(root).as_posix(), "sha256=" + digest, len(data)))
    writer.writerow(((info / "RECORD").relative_to(root).as_posix(), "", ""))
    (info / "RECORD").write_text(record.getvalue())


def _source(root, kind, callback):
    calls = root / "scientific-calls"
    source = f'''from pathlib import Path
from bioimageflow_core import ProcessingTool, IOModel, EnvironmentSpec, RowConsumption
class BaseTool(ProcessingTool):
    row_consumption = RowConsumption.MAPPED
    environment = EnvironmentSpec("receipt-environment", {{"python": ">=3.9"}})
    class Inputs(IOModel): pass
    class Outputs(IOModel): value: int
    def process_row(self, arguments):
        with Path({str(calls)!r}).open("a") as stream:
            stream.write("1\\n")
        return self.Outputs(value=1)
'''
    if callback == "batch":
        source += '''    def process_batch(self, arguments_list):
        return [self.process_row(arguments) for arguments in arguments_list]
'''
    if kind == "source":
        source += "class WitnessTool(BaseTool):\n    row_consumption = BaseTool.row_consumption\n"
        path = root / "standalone.py"
        path.write_text(source)
        return path
    selected = root / "store" / "selected_primary_tools" / "1.0.0" if kind == "versioned" else root / "shared"
    package = selected / "selected_primary_tools"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("from .tool import WitnessTool\n")
    (package / "tool.py").write_text("from .base import BaseTool\nclass WitnessTool(BaseTool):\n    row_consumption = BaseTool.row_consumption\n")
    path = package / "base.py"
    path.write_text(source)
    if kind == "versioned":
        _write_metadata(selected)
    return path


def _controller(root, kind, stale):
    """A fresh ordinary controller; no production namespace eviction or reload."""
    sys.dont_write_bytecode = True
    root = Path(root)
    if kind == "source":
        spec = importlib.util.spec_from_file_location("selected_primary_standalone", root / "standalone.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
    elif kind == "shared":
        sys.path.insert(0, str(root / "shared"))
        module = importlib.import_module("selected_primary_tools.tool")
    else:
        module = load_versioned_package("selected_primary_tools", "1.0.0", root / "store")
    tool_type = module.WitnessTool
    dependency = SimpleNamespace(value=1)
    provider = _Provider(root / "managed", dependency)
    env_manager.get_shared_environment_manager = lambda **kwargs: provider
    pin = "bioimageflow-core==" + importlib.metadata.version("bioimageflow-core")
    manager = env_manager.WetlandsEnvManager(root=provider.root, bioimageflow_core_dependency=pin)
    provider.selected = _Environment(provider, EnvironmentSpec(python=">=3.9", pypi=(pin,)).recipe_hash)
    with Workflow(engine="wetlands", execution="sequential", storage_path=root / "records") as workflow:
        node = tool_type()(name="subject")
    engine = workflow.create_engine(resource_lifetime="external", env_manager=manager)
    owner = workflow.shared_memory_context

    def compute():
        context = WorkflowExecutionContext()
        frame = workflow.compute(node, engine=engine, run_context=context)
        [outcome] = context.execution_outcomes
        return {"values": frame["value"].tolist(), "key": outcome.result_key, "record": outcome.record_id}

    try:
        first, warm = compute(), compute()
        assert first == warm
        assert provider.submissions == 1
        if stale:
            path = root / "standalone.py" if kind == "source" else root / ("shared" if kind == "shared" else "store/selected_primary_tools/1.0.0") / "selected_primary_tools/base.py"
            path.write_text(path.read_text().replace('stream.write("1\\n")', 'stream.write("9\\n")').replace("value=1", "value=9"))
            if kind == "versioned":
                _write_metadata(path.parent.parent)
            namespace = {name: item for name, item in sys.modules.items() if item is module or name.startswith(tool_type.__module__.split(".")[0] + ".")}
            before = (provider.submissions, provider.starts, provider.receipt_reads)
            original = Storage.load_current
            def forbidden_lookup(*args, **kwargs):
                raise AssertionError("Unadmitted primary reached reusable lookup")
            Storage.load_current = forbidden_lookup
            try:
                for operation in (lambda: workflow.plan(engine=engine), compute):
                    with pytest.raises((ImportError, ValueError), match="Resident/source|primary|Primary|admitted"):
                        operation()
            finally:
                Storage.load_current = original
            assert (provider.submissions, provider.starts, provider.receipt_reads) == before
            assert all(sys.modules[name] is item for name, item in namespace.items())
        return first
    finally:
        engine.close()
        manager.shutdown_all()
        assert owner.close().state == "closed"


def _fresh_controller(root, kind, stale):
    code = "import json; from tests.integration.runtime_cache.test_selected_primary_identity import _controller; print(json.dumps(_controller(" + repr(str(root)) + ", " + repr(kind) + ", " + repr(stale) + ")))"
    result = subprocess.run([sys.executable, "-c", code], cwd=Path(__file__).parents[3], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    return json.loads(result.stdout.splitlines()[-1])


@pytest.mark.parametrize("kind", ["source", "shared", "versioned"])
@pytest.mark.parametrize("callback", ["row", "batch"])
def test_selected_inherited_primary_refuses_stale_controller_and_clean_b_reuses(tmp_path, kind, callback):
    _source(tmp_path, kind, callback)
    first = _fresh_controller(tmp_path, kind, True)
    assert first["values"] == [1]
    changed = _fresh_controller(tmp_path, kind, False)
    assert changed["values"] == [9]
    assert first["key"] != changed["key"]
    assert (tmp_path / "scientific-calls").read_text().splitlines() == ["1", "9"]
