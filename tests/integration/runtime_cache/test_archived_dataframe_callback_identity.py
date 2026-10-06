"""An unchanged archive retains actual inherited Direct DataFrame callbacks."""

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import subprocess
import sys


def _controller(root, storage_path):
    sys.dont_write_bytecode = True
    root = Path(root)
    sys.path.insert(0, str(root / "external"))
    from bioimageflow import Workflow, WorkflowExecutionContext
    from bioimageflow.storage import Storage

    archive = json.loads((root / "archive.json").read_text())
    workflow = Workflow.from_dict(archive, storage_path=storage_path, engine="direct")
    results = []
    for _ in range(2):
        context = WorkflowExecutionContext()
        frame = workflow.compute(run_context=context)
        record = Storage(storage_path).read_run_node_result(context.run_id, "collision")
        results.append(dict(values=frame["value"].tolist(), key=record.result_key,
            record=record.record_id, cache_hit=record.cache_hit))
    return results


def test_archived_dataframe_inherited_transform_selects_actual_callback_record(tmp_path):
    from bioimageflow.storage import Storage

    fixture = json.loads(Path("tests/fixtures/unified_workflow_archive.json").read_text())
    archive = {"archive_version": fixture["archive_version"],
        "workflow": deepcopy(fixture["workflow"]["nodes"][0]["workflow"]),
        "custom_sources": [deepcopy(fixture["custom_sources"][0])]}
    source = archive["custom_sources"][0]
    text = "from external_owner import Base\nclass CollisionTool(Base):\n    pass\n"
    source.update(source=text, source_hash=hashlib.sha256(text.encode()).hexdigest())
    archive_path = tmp_path / "archive.json"
    archive_path.write_text(json.dumps(archive, sort_keys=True))
    captured_archive = archive_path.read_bytes()
    external = tmp_path / "external"
    external.mkdir()
    audit = tmp_path / "scientific-calls"
    body = f'''from pathlib import Path
import pandas as pd
from bioimageflow import DataFrameTool
from bioimageflow_core import IOModel
class Base(DataFrameTool):
    accepts_upstream = False
    class Inputs(IOModel): pass
    class Outputs(IOModel): value: int
    def transform(self, df, arguments):
        with Path({str(audit)!r}).open("a") as stream:
            stream.write("1\\n")
        return pd.DataFrame({{"value": [1]}}, index=["row"])
'''
    owner = external / "external_owner.py"
    owner.write_text(body)
    child = '''import json, sys
from tests.integration.runtime_cache.test_archived_dataframe_callback_identity import _controller
print(json.dumps(_controller(sys.argv[1], sys.argv[2])))
'''

    def compute(storage_path):
        result = subprocess.run([sys.executable, "-c", child, str(tmp_path), str(storage_path)], capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout)

    storage_path = tmp_path / "shared-records"
    first, warm_first = compute(storage_path)
    assert first["values"] == warm_first["values"] == [1]
    assert not first["cache_hit"] and warm_first["cache_hit"]
    assert (first["key"], first["record"]) == (warm_first["key"], warm_first["record"])
    assert audit.read_text().splitlines() == ["1"]
    old_manifest = Storage(storage_path).load_record_manifest(first["key"], first["record"])
    owner.write_text(body.replace('stream.write("1\\n")', 'stream.write("9\\n")').replace("[1]", "[9]"))
    independent, warm_independent = compute(tmp_path / "fresh-records")
    assert independent["values"] == warm_independent["values"] == [9]
    assert not independent["cache_hit"] and warm_independent["cache_hit"]
    assert independent["key"] == warm_independent["key"] and independent["record"] == warm_independent["record"]
    assert audit.read_text().splitlines() == ["1", "9"]
    changed, warm_changed = compute(storage_path)
    assert changed["values"] == warm_changed["values"] == [9]
    assert not changed["cache_hit"] and warm_changed["cache_hit"]
    assert changed["key"] != first["key"] and changed["record"] != first["record"]
    assert changed["key"] == independent["key"]
    assert (changed["key"], changed["record"]) == (warm_changed["key"], warm_changed["record"])
    assert audit.read_text().splitlines() == ["1", "9", "9"]
    storage = Storage(storage_path)
    assert storage.load_record_manifest(first["key"], first["record"]) == old_manifest
    assert storage.load_record_dataframe(first["key"], first["record"])["value"].tolist() == [1]
    assert storage.load_current(first["key"]).record_id == first["record"]
    assert archive_path.read_bytes() == captured_archive
    assert source["source_hash"] == hashlib.sha256(text.encode()).hexdigest()
