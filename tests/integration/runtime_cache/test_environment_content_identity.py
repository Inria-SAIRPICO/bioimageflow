"""Workflow cache admission through a fake public ready-runtime provider.

The canonical SDK executes real tasks in-process; these controls do not certify
Pixi provisioning or physical worker processes.
"""

import hashlib
import importlib.metadata
import sys
from types import ModuleType, SimpleNamespace

import pytest
from wetlands import EnvironmentNotReadyError, EnvironmentSpec, ExecutionState

from bioimageflow import Workflow, WorkflowExecutionContext
from bioimageflow import env_manager
from bioimageflow.storage import Storage
from bioimageflow_core.worker import execute_processing_task


class _Operation:
    def __init__(self, environment):
        self.environment = environment

    def wait_for(self, timeout=None):
        return self.environment

    def listen(self, callback):
        pass


class _Task:
    state = ExecutionState.COMPLETED

    def __init__(self, payload):
        self.result = execute_processing_task(payload)

    def wait_for(self, timeout=None):
        return self.result

    def listen(self, callback):
        pass


class _Pool:
    def __init__(self, environment):
        self.environment = environment
        self.closed = False

    def submit_import(self, target, *, args, context_keyword):
        assert target == "bioimageflow_core.worker:execute_processing_task"
        assert context_keyword == "task"
        assert not self.closed
        self.environment.provider.submissions += 1
        return _Task(args[0])

    def close(self):
        self.closed = True


class _Environment:
    def __init__(self, provider, recipe_hash, *, generation_id="generation-one"):
        self.provider = provider
        self.name = "receipt-environment"
        self.recipe_hash = recipe_hash
        self.lockfile_hash = "b" * 64
        self.generation_id = generation_id

    def runtime_content_receipt(self):
        from wetlands import RuntimeContentReceipt

        self.provider.receipt_reads += 1
        facts = {
            "schema_version": 1,
            "python": {
                "implementation": "cpython", "version": [3, 12, 14],
                "cache_tag": "cpython-312", "soabi": "cpython-312-darwin",
                "platform": "darwin", "machine": "arm64", "executable_digest": "0" * 64,
            },
            "distributions": [{
                "name": "receipt-dependency", "version": "1.0.0",
                "content_digest": hashlib.sha256(str(self.provider.dependency.value).encode()).hexdigest(),
            }],
            "resolved_artifacts": [],
            "editable_sources": [],
        }
        return RuntimeContentReceipt(
            generation_id=self.generation_id, recipe_hash=self.recipe_hash,
            lockfile_hash=self.lockfile_hash, scientific_facts=facts,
        )

    def start(self, **kwargs):
        self.provider.starts += 1
        return _Pool(self)

    def run(self, *args, **kwargs):
        raise AssertionError("Ready admission/planning must not spawn a content probe")


class _Provider:
    def __init__(self, root, dependency):
        self.root = root
        self.dependency = dependency
        self.selected = None
        self.provisions = []
        self.starts = 0
        self.submissions = 0
        self.receipt_reads = 0

    def managed_environments(self):
        if self.selected is None:
            return ()
        return (SimpleNamespace(
            name=self.selected.name, ready=True, recipe_hash=self.selected.recipe_hash,
        ),)

    def environment(self, name):
        if self.selected is None or self.selected.name != name:
            raise EnvironmentNotReadyError(f"Environment {name!r} is not ready")
        return self.selected

    def provision(self, name, spec, *, replace_existing=False):
        assert name == "receipt-environment" and not replace_existing
        self.provisions.append(spec)
        if self.selected is None:
            self.selected = _Environment(self, spec.recipe_hash)
        assert self.selected.recipe_hash == spec.recipe_hash
        return _Operation(self.selected)

    def select(self, *, value, generation_id):
        assert self.selected is not None
        self.dependency.value = value
        self.selected = _Environment(self, self.selected.recipe_hash, generation_id=generation_id)


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    suffix = hashlib.sha256(str(tmp_path).encode()).hexdigest()[:12]
    dependency = ModuleType("receipt_dependency_" + suffix)
    dependency.value = 1
    dependency.calls = []
    monkeypatch.setitem(sys.modules, dependency.__name__, dependency)
    path = tmp_path / "receipt_tool.py"
    source = f'''from bioimageflow_core import EnvironmentSpec, IOModel, ProcessingTool, RowConsumption
class ReceiptTool(ProcessingTool):
    environment = EnvironmentSpec("receipt-environment", {{"python": ">=3.9"}})
    row_consumption = RowConsumption.MAPPED
    class Inputs(IOModel): pass
    class Outputs(IOModel): value: int
    def process_row(self, arguments):
        import {dependency.__name__} as dependency
        dependency.calls.append(dependency.value)
        return self.Outputs(value=dependency.value)
'''
    path.write_text(source)
    module = ModuleType("receipt_tool_" + suffix)
    module.__file__ = str(path)
    monkeypatch.setitem(sys.modules, module.__name__, module)
    exec(compile(source, str(path), "exec", dont_inherit=True), module.__dict__)
    provider = _Provider(tmp_path / "managed", dependency)
    monkeypatch.setattr(env_manager, "get_shared_environment_manager", lambda **kwargs: provider)
    core_pin = "bioimageflow-core==" + importlib.metadata.version("bioimageflow-core")
    manager = env_manager.WetlandsEnvManager(root=provider.root, bioimageflow_core_dependency=core_pin)
    augmented = EnvironmentSpec(python=">=3.9", pypi=(core_pin,))
    provider.selected = _Environment(provider, augmented.recipe_hash)
    owners = []
    engines = []

    def workflow(*, backend="wetlands", names=("subject",)):
        with Workflow(engine=backend, execution="sequential", storage_path=tmp_path / "records") as graph:
            nodes = tuple(module.ReceiptTool()(name=name) for name in names)
        owners.append(graph.shared_memory_context)
        engine = graph.create_engine(resource_lifetime="external", env_manager=manager) if backend == "wetlands" else graph.create_engine()
        engines.append(engine)
        return graph, nodes, engine

    yield SimpleNamespace(provider=provider, manager=manager, dependency=dependency,
        workflow=workflow, core_pin=core_pin)
    for engine in engines:
        engine.close()
    manager.shutdown_all()
    for owner in owners:
        assert owner.close().state == "closed"


def _compute(graph, nodes, engine):
    context = WorkflowExecutionContext()
    frame = graph.compute(*nodes, engine=engine, run_context=context)
    selected = {}
    storage = Storage(graph.storage_path)
    for outcome in context.execution_outcomes:
        assert outcome.result_key is not None and outcome.record_id is not None
        pointer = storage.load_current(outcome.result_key)
        assert pointer is not None and pointer.record_id == outcome.record_id
        selected[outcome.node_key] = (outcome.result_key, outcome.record_id)
    return frame, selected


def test_changed_content_same_recipe_and_lock_selects_its_actual_record(runtime):
    graph, nodes, engine = runtime.workflow()
    first, selected_first = _compute(graph, nodes, engine)
    recipe, lock = runtime.provider.selected.recipe_hash, runtime.provider.selected.lockfile_hash
    assert runtime.manager.retire_idle("receipt-environment")
    runtime.provider.select(value=9, generation_id="generation-nine")
    assert (runtime.provider.selected.recipe_hash, runtime.provider.selected.lockfile_hash) == (recipe, lock)
    changed, selected_changed = _compute(graph, nodes, engine)
    assert first["value"].tolist() == [1]
    assert changed["value"].tolist() == [9]
    assert selected_first["subject"][0] != selected_changed["subject"][0]
    assert runtime.dependency.calls == [1, 9]


def test_identical_content_new_generation_reuses_with_pool_closed(runtime):
    graph, nodes, engine = runtime.workflow()
    first, selected_first = _compute(graph, nodes, engine)
    assert runtime.manager.retire_idle("receipt-environment")
    starts, submissions = runtime.provider.starts, runtime.provider.submissions
    planned = graph.plan(engine=engine)["subject"]
    assert planned.cached and (planned.final_result_key, planned.selected_record_id) == selected_first["subject"]
    assert runtime.provider.starts == starts and runtime.provider.submissions == submissions
    warm, selected_warm = _compute(graph, nodes, engine)
    assert runtime.provider.starts == starts and runtime.provider.submissions == submissions
    runtime.provider.select(value=1, generation_id="identical-new-generation")
    rebuilt, selected_rebuilt = _compute(graph, nodes, engine)
    assert first["value"].tolist() == warm["value"].tolist() == rebuilt["value"].tolist() == [1]
    assert selected_first == selected_warm == selected_rebuilt
    assert runtime.provider.starts == starts and runtime.provider.submissions == submissions
    assert runtime.dependency.calls == [1]


def test_missing_runtime_plan_is_pending_without_provision_start_or_probe(runtime):
    graph, nodes, engine = runtime.workflow()
    runtime.provider.selected = None
    assert not runtime.provider.root.exists()
    before = (len(runtime.provider.provisions), runtime.provider.starts, runtime.provider.submissions, runtime.provider.receipt_reads)
    plan = graph.plan(engine=engine)
    assert plan["subject"].status.value == "pending_runtime"
    assert plan["subject"].final_result_key is None and plan["subject"].selected_record_id is None
    assert before == (len(runtime.provider.provisions), runtime.provider.starts, runtime.provider.submissions, runtime.provider.receipt_reads)
    assert not runtime.provider.root.exists()
    assert runtime.dependency.calls == []


def test_direct_reuses_without_managed_runtime_admission(runtime):
    graph, nodes, engine = runtime.workflow(backend="direct")
    runtime.provider.selected = None
    first, selected_first = _compute(graph, nodes, engine)
    warm, selected_warm = _compute(graph, nodes, engine)
    assert first["value"].tolist() == warm["value"].tolist() == [1]
    assert selected_first == selected_warm and runtime.dependency.calls == [1]
    assert runtime.provider.receipt_reads == runtime.provider.starts == runtime.provider.submissions == 0
    assert runtime.provider.provisions == []


def test_shared_augmented_recipe_admits_once_per_operation_and_refreshes_next(runtime):
    graph, nodes, engine = runtime.workflow(names=("left", "right"))
    _, selected = _compute(graph, nodes, engine)
    assert set(selected) == {"left", "right"}
    assert runtime.provider.receipt_reads == 1
    # The initially ready recipe includes the authoritative Core augmentation;
    # the declared tool recipe alone cannot match that public ready selection.
    assert runtime.provider.provisions == []
    assert runtime.manager.retire_idle("receipt-environment")
    runtime.provider.select(value=9, generation_id="new-operation-nine")
    _, changed = _compute(graph, nodes, engine)
    assert runtime.provider.receipt_reads == 2
    assert all(selected[name][0] != changed[name][0] for name in selected)
    assert runtime.dependency.calls == [1, 1, 9, 9]


def test_active_steps_refuse_same_engine_plan_without_erasing_admission(runtime):
    graph, nodes, engine = runtime.workflow(names=("left", "right"))
    context = WorkflowExecutionContext()
    steps = graph.compute_steps(*nodes, engine=engine, run_context=context)
    try:
        first = next(steps)
        assert not first.cached
        assert runtime.provider.receipt_reads == 1
        with pytest.raises(RuntimeError, match="already has an active execution"):
            graph.plan(engine=engine)
        assert runtime.provider.receipt_reads == 1
        assert first.execute()["value"].tolist() == [1]
        for step in steps:
            assert step.execute()["value"].tolist() == [1]
        assert runtime.provider.receipt_reads == 1
    finally:
        steps.close()
    assert context.terminal_status == "succeeded"
    assert all(entry.cached for entry in graph.plan(engine=engine).values())
    assert runtime.provider.receipt_reads == 2
    _compute(graph, nodes, engine)
    assert runtime.provider.receipt_reads == 3
    assert runtime.dependency.calls == [1, 1]


def test_later_cached_step_refuses_replaced_generation_without_content_rescan(runtime):
    graph, nodes, engine = runtime.workflow(names=("left", "right"))
    _, selected = _compute(graph, nodes, engine)
    assert runtime.manager.retire_idle("receipt-environment")
    before = (runtime.provider.starts, runtime.provider.submissions)
    context = WorkflowExecutionContext()
    steps = graph.compute_steps(*nodes, engine=engine, run_context=context)
    try:
        first = next(steps)
        assert first.cached
        assert first.execute()["value"].tolist() == [1]
        later = next(steps)
        assert later.cached
        reads = runtime.provider.receipt_reads
        runtime.provider.select(value=1, generation_id="replacement-between-steps")
        replacement = runtime.provider.selected
        with pytest.raises(RuntimeError, match="changed after runtime admission"):
            _ = later.cached
        with pytest.raises(RuntimeError, match="changed after runtime admission"):
            later.execute()
        assert first.cached
        assert first.execute()["value"].tolist() == [1]
        assert runtime.provider.receipt_reads == reads
        assert runtime.provider.selected is replacement
        assert (runtime.provider.starts, runtime.provider.submissions) == before
        assert runtime.dependency.calls == [1, 1]
        storage = Storage(graph.storage_path)
        for key, record_id in selected.values():
            assert storage.load_current(key).record_id == record_id
    finally:
        steps.close()
    with pytest.raises(RuntimeError, match="changed after runtime admission"):
        _ = later.cached
    with pytest.raises(RuntimeError, match="changed after runtime admission"):
        later.execute()
    assert runtime.provider.receipt_reads == reads
    assert runtime.provider.selected is replacement
    assert (runtime.provider.starts, runtime.provider.submissions) == before
    assert not runtime.manager.retire_idle("receipt-environment")
    fresh, selected_fresh = _compute(graph, nodes, engine)
    assert set(fresh) == {"left", "right"}
    assert all(frame["value"].tolist() == [1] for frame in fresh.values())
    assert selected_fresh == selected
    assert runtime.provider.receipt_reads == reads + 1
    assert (runtime.provider.starts, runtime.provider.submissions) == before
    assert runtime.dependency.calls == [1, 1]
