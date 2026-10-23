"""Installed generation reuse, exact selection and admitted-operation lifetime."""

import shutil
from pathlib import Path

import pytest

from bioimageflow.cache.identity import deterministic_serialize
from bioimageflow.tool_loader import load_versioned_package, resolve_tool_class, unload_versioned_package
from bioimageflow.worker_origins import ExecutableMetadata, capture_tool_executable

pytest_plugins = ("tests.testkit.tool_loader",)


def _capture(tool, metadata):
    return capture_tool_executable(tool, managed=True, canonicalize=deterministic_serialize, metadata=metadata)


def test_warm_generations_reuse_admission_without_metadata_source_or_attestation(tool_store, monkeypatch):
    import bioimageflow.tool_loader as loader
    import bioimageflow.worker_origins as origins
    from bioimageflow_core.primary_content import PrimaryContentAdmission

    module = load_versioned_package("dummy_tools", "1.0.0", tool_store)
    metadata = ExecutableMetadata()
    first = dict(_capture(module.AlphaTool(), metadata).scientific_key)
    metadata.clear()
    calls = []

    def forbidden(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("Warm admitted generation repeated installation/source admission")

    monkeypatch.setattr(loader, "admit_installation", forbidden)
    monkeypatch.setattr(loader, "admit_import_root", forbidden)
    monkeypatch.setattr(loader, "capture_primary_package", forbidden)
    monkeypatch.setattr(origins.importlib.metadata, "distributions", forbidden)
    monkeypatch.setattr(origins.importlib.metadata, "packages_distributions", forbidden)
    read_bytes = Path.read_bytes
    attest = PrimaryContentAdmission.attest
    generation = module.AlphaTool._bif_installation_generation
    fixed = generation.classes[module.AlphaTool]["fixed"]
    def admitted_read(path):
        if path in generation.primary.paths.values():
            return forbidden(path)
        return read_bytes(path)
    def admitted_attest(admission, tool):
        if fixed:
            return forbidden(admission, tool)
        # The source checkout has editable inherited framework owners. Those
        # owners retain operation freshness; installed candidate artifacts have
        # actual RECORD owners and take the zero-attestation path above.
        return attest(admission, tool)
    monkeypatch.setattr(Path, "read_bytes", admitted_read)
    monkeypatch.setattr(PrimaryContentAdmission, "attest", admitted_attest)
    try:
        for _ in range(3):
            assert load_versioned_package("dummy_tools", "1.0.0", tool_store) is module
            klass = resolve_tool_class("dummy_tools", "1.0.0", "dummy_tools.alpha", "AlphaTool", store_path=tool_store)
            assert dict(_capture(klass(), metadata).scientific_key) == first
            metadata.clear()
        assert calls == []
    finally:
        metadata.clear()


def test_unload_refuses_active_lease_then_retires_old_classes(tool_store):
    module = load_versioned_package("dummy_tools", "1.0.0", tool_store)
    metadata = ExecutableMetadata()
    first = _capture(module.AlphaTool(), metadata)
    with pytest.raises(RuntimeError, match="active admitted operations"):
        unload_versioned_package("dummy_tools", "1.0.0", tool_store)
    assert load_versioned_package("dummy_tools", "1.0.0", tool_store) is module
    metadata.clear()
    unload_versioned_package("dummy_tools", "1.0.0", tool_store)
    with pytest.raises(RuntimeError, match="retired.*recompile"):
        _capture(module.AlphaTool(), metadata)
    replacement = load_versioned_package("dummy_tools", "1.0.0", tool_store)
    assert replacement.AlphaTool is not module.AlphaTool
    try:
        assert dict(_capture(replacement.AlphaTool(), metadata).scientific_key) == dict(first.scientific_key)
    finally:
        metadata.clear()


def test_exact_store_lookup_and_retirement_never_borrow_resident_generation(tool_store, tmp_path):
    second = tmp_path / "other_store"
    shutil.copytree(tool_store, second)
    module = load_versioned_package("dummy_tools", "1.0.0", tool_store)
    with pytest.raises(ImportError, match="store root"):
        resolve_tool_class("dummy_tools", "1.0.0", "dummy_tools.alpha", "AlphaTool", store_path=second)
    with pytest.raises(ImportError, match="store root"):
        unload_versioned_package("dummy_tools", "1.0.0", second)
    assert load_versioned_package("dummy_tools", "1.0.0", tool_store) is module
    unload_versioned_package("dummy_tools", "1.0.0", tool_store)
    replacement = load_versioned_package("dummy_tools", "1.0.0", second)
    assert replacement.AlphaTool is not module.AlphaTool


def test_per_instance_declarations_and_callback_facts_stay_fresh(tool_store):
    from bioimageflow_core import IOModel

    module = load_versioned_package("dummy_tools", "1.0.0", tool_store)
    metadata = ExecutableMetadata()
    tool = module.AlphaTool()
    try:
        first_capture = _capture(tool, metadata)
        first = dict(first_capture.scientific_key)

        class Changed(IOModel):
            result: int

        tool.Outputs = Changed
        changed = dict(_capture(tool, metadata).scientific_key)
        assert changed["declaration"] != first["declaration"]
        other = module.AlphaTool()
        other_capture = _capture(other, metadata)
        assert other_capture.callbacks["process_row"].__self__ is other
        assert first_capture.callbacks["process_row"].__self__ is tool
    finally:
        metadata.clear()


def test_warm_node_plans_reuse_generation_and_run_no_callback(tool_store, monkeypatch, tmp_path):
    from bioimageflow import Workflow
    import bioimageflow.tool_loader as loader
    from bioimageflow_core.primary_content import PrimaryContentAdmission

    module = load_versioned_package("dummy_tools", "1.0.0", tool_store)
    tool = module.AlphaTool()
    callback = tool.process_row
    calls = []
    def observed_callback(arguments):
        calls.append(arguments)
        return callback(arguments)
    tool.process_row = observed_callback
    with Workflow(engine="direct", storage_path=tmp_path / "records") as workflow:
        tool(name="subject")
    first = workflow.plan()["subject"]
    observed = []

    def forbidden(*args, **kwargs):
        observed.append(args)
        raise AssertionError("Warm plan repeated installation/source/callback admission")

    monkeypatch.setattr(loader, "admit_installation", forbidden)
    monkeypatch.setattr(loader, "admit_import_root", forbidden)
    monkeypatch.setattr(loader, "capture_primary_package", forbidden)
    monkeypatch.setattr(Path, "read_bytes", forbidden)
    monkeypatch.setattr(PrimaryContentAdmission, "attest", forbidden)
    monkeypatch.setattr(loader.importlib.metadata, "packages_distributions", forbidden)
    for _ in range(3):
        planned = workflow.plan()["subject"]
        assert planned.logical_signature == first.logical_signature
        assert planned.status == first.status
    assert observed == [] and calls == []


def test_identical_reload_preserves_real_cache_selection(tool_store, tmp_path):
    from bioimageflow import Workflow

    store = tmp_path / "records"
    module = load_versioned_package("dummy_tools", "1.0.0", tool_store)
    with Workflow(engine="direct", storage_path=store) as workflow:
        node = module.AlphaTool()(name="subject")
    assert workflow.compute(node)["result"].tolist() == ["v1"]
    first = workflow.plan()["subject"]
    assert first.cached
    unload_versioned_package("dummy_tools", "1.0.0", tool_store)
    replacement = load_versioned_package("dummy_tools", "1.0.0", tool_store)
    with Workflow(engine="direct", storage_path=store) as workflow:
        replacement.AlphaTool()(name="subject")
    second = workflow.plan()["subject"]
    assert second.cached
    assert (second.logical_signature, second.final_result_key, second.selected_record_id) == (
        first.logical_signature, first.final_result_key, first.selected_record_id,
    )


def test_compute_steps_owns_generation_until_engine_execution_finishes(tool_store, tmp_path):
    from bioimageflow import Workflow

    module = load_versioned_package("dummy_tools", "1.0.0", tool_store)
    with Workflow(engine="direct", storage_path=tmp_path / "records") as workflow:
        node = module.AlphaTool()(name="subject")
    steps = workflow.compute_steps(node)
    try:
        step = next(steps)
        with pytest.raises(RuntimeError, match="active admitted operations"):
            unload_versioned_package("dummy_tools", "1.0.0", tool_store)
        assert step.execute()["result"].tolist() == ["v1"]
    finally:
        steps.close()
    unload_versioned_package("dummy_tools", "1.0.0", tool_store)
    assert load_versioned_package("dummy_tools", "1.0.0", tool_store).AlphaTool is not module.AlphaTool


def test_batch_unload_refuses_all_when_one_generation_is_active(tool_store):
    from bioimageflow.tool_loader import unload_versioned_packages

    first = load_versioned_package("dummy_tools", "1.0.0", tool_store)
    second = load_versioned_package("dummy_tools", "2.0.0", tool_store)
    metadata = ExecutableMetadata()
    _capture(second.AlphaTool(), metadata)
    selected = [("dummy_tools", "1.0.0"), ("dummy_tools", "2.0.0"), ("dummy_tools", "1.0.0")]
    try:
        with pytest.raises(RuntimeError, match="active admitted operations"):
            unload_versioned_packages(selected, tool_store)
        assert load_versioned_package("dummy_tools", "1.0.0", tool_store) is first
        assert load_versioned_package("dummy_tools", "2.0.0", tool_store) is second
    finally:
        metadata.clear()
    unload_versioned_packages(selected, tool_store)
    assert load_versioned_package("dummy_tools", "1.0.0", tool_store) is not first
    assert load_versioned_package("dummy_tools", "2.0.0", tool_store) is not second


def test_batch_unload_root_mismatch_preserves_every_generation(tool_store, tmp_path):
    from bioimageflow.tool_loader import unload_versioned_packages

    first = load_versioned_package("dummy_tools", "1.0.0", tool_store)
    other = tmp_path / "other_store"
    shutil.copytree(tool_store, other)
    second = load_versioned_package("dummy_tools", "2.0.0", other)
    with pytest.raises(ImportError, match="store root"):
        unload_versioned_packages([("dummy_tools", "1.0.0"), ("dummy_tools", "2.0.0")], tool_store)
    assert load_versioned_package("dummy_tools", "1.0.0", tool_store) is first
    assert load_versioned_package("dummy_tools", "2.0.0", other) is second


def test_pending_upstream_plan_keeps_diagnostic_without_full_executable_capture(tool_store, tmp_path, monkeypatch):
    from bioimageflow import Workflow

    module = load_versioned_package("dummy_tools", "1.0.0", tool_store)
    with Workflow(engine="direct", storage_path=tmp_path / "records") as workflow:
        source = module.LoaderTool()(name="source", path=str(tmp_path))
        downstream = module.LoaderTool()(source, name="downstream", path=str(tmp_path))
    engine = workflow.create_engine()
    captures = []
    capture = engine._capture_executable
    def observed_capture(node):
        assert node is not downstream, "Unresolved upstream requested full executable admission"
        captures.append(node)
        return capture(node)
    monkeypatch.setattr(engine, "_capture_executable", observed_capture)
    try:
        plan = workflow.plan(engine=engine)
        assert plan["downstream"].status.value == "pending_upstream"
        assert plan["downstream"].logical_signature
        assert plan["downstream"].final_result_key is None
        assert captures == [source]
    finally:
        engine.close()


def test_editable_inherited_owner_refreshes_per_operation_without_selected_reads(tool_store, tmp_path, monkeypatch):
    import sys
    from types import ModuleType
    from tests.testkit.tool_loader import record_distribution

    path = tmp_path / "editable_parent.py"
    source = '''from bioimageflow_core import ProcessingTool
class Parent(ProcessingTool):
    def __init__(self):
        super().__init__()
        self.editable_marker = 1
'''
    path.write_text(source)
    parent = ModuleType("editable_parent")
    parent.__file__ = str(path)
    monkeypatch.setitem(sys.modules, parent.__name__, parent)
    exec(compile(source, str(path), "exec", dont_inherit=True), parent.__dict__)
    root = tool_store / "dummy_tools" / "1.0.0"
    base = root / "dummy_tools" / "base.py"
    base.write_text(base.read_text().replace(
        "from bioimageflow_core import ProcessingTool, EnvironmentSpec",
        "from bioimageflow_core import EnvironmentSpec\nfrom editable_parent import Parent as ProcessingTool",
    ))
    record_distribution(root, "dummy_tools", "1.0.0")
    module = load_versioned_package("dummy_tools", "1.0.0", tool_store)
    metadata = ExecutableMetadata()
    reads = []
    read_bytes = Path.read_bytes
    selected = set(module.AlphaTool._bif_installation_generation.primary.paths.values())
    def observed_read(member):
        assert member not in selected, "Selected installed bytes were reread within the admitted generation"
        if member == path:
            reads.append(member)
        return read_bytes(member)
    monkeypatch.setattr(Path, "read_bytes", observed_read)
    try:
        first = _capture(module.AlphaTool(), metadata)
        assert _capture(module.AlphaTool(), metadata).scientific_key == first.scientific_key
        assert reads == [path]
        path.write_text(source.replace("editable_marker = 1", "editable_marker = 9"))
        # The already admitted operation holds its original editable bytes.
        assert _capture(module.AlphaTool(), metadata).scientific_key == first.scientific_key
        assert reads == [path]
        metadata.clear()
        # A later operation cannot silently retain the old inherited owner.
        with pytest.raises(ValueError, match="Resident/source"):
            _capture(module.AlphaTool(), metadata)
        assert reads == [path, path]
    finally:
        metadata.clear()


def test_mutation_guard_refuses_active_selection_without_retirement(tool_store):
    from bioimageflow.tool_loader import versioned_package_mutation

    module = load_versioned_package("dummy_tools", "1.0.0", tool_store)
    metadata = ExecutableMetadata()
    _capture(module.AlphaTool(), metadata)
    try:
        with pytest.raises(RuntimeError, match="active admitted operations"):
            with versioned_package_mutation([("dummy_tools", "1.0.0")], tool_store):
                pytest.fail("Owned generation entered physical mutation")
        assert load_versioned_package("dummy_tools", "1.0.0", tool_store) is module
    finally:
        metadata.clear()


def test_mutation_guard_blocks_concurrent_admission_without_holding_thread_lock(tool_store):
    from concurrent.futures import ThreadPoolExecutor
    from bioimageflow.tool_loader import versioned_package_mutation

    module = load_versioned_package("dummy_tools", "1.0.0", tool_store)
    with ThreadPoolExecutor(max_workers=1) as executor:
        with versioned_package_mutation([("dummy_tools", "1.0.0")], tool_store):
            def concurrent_read():
                with pytest.raises(RuntimeError, match="mutation is in progress"):
                    load_versioned_package("dummy_tools", "1.0.0", tool_store)
                with pytest.raises(RuntimeError, match="mutation is in progress"):
                    resolve_tool_class("dummy_tools", "1.0.0", "dummy_tools.alpha", "AlphaTool", store_path=tool_store)
                with pytest.raises(RuntimeError, match="mutation is in progress"):
                    unload_versioned_package("dummy_tools", "1.0.0", tool_store)
                return load_versioned_package("dummy_tools", "2.0.0", tool_store)
            other = executor.submit(concurrent_read).result(timeout=5)
            assert other.AlphaTool._bif_package_version == "2.0.0"
        assert load_versioned_package("dummy_tools", "1.0.0", tool_store).AlphaTool is not module.AlphaTool


def test_mutation_guard_failure_releases_reservation_but_keeps_old_class_retired(tool_store):
    from bioimageflow.tool_loader import versioned_package_mutation

    module = load_versioned_package("dummy_tools", "1.0.0", tool_store)
    with pytest.raises(OSError, match="physical removal failed"):
        with versioned_package_mutation([("dummy_tools", "1.0.0")], tool_store):
            with pytest.raises(RuntimeError, match="mutation is in progress"):
                with versioned_package_mutation([("dummy_tools", "1.0.0")], tool_store):
                    pytest.fail("Overlapping mutation entered")
            raise OSError("physical removal failed")
    metadata = ExecutableMetadata()
    try:
        with pytest.raises(RuntimeError, match="retired.*recompile"):
            _capture(module.AlphaTool(), metadata)
        assert load_versioned_package("dummy_tools", "1.0.0", tool_store).AlphaTool is not module.AlphaTool
    finally:
        metadata.clear()
