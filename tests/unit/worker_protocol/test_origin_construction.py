"""Orchestrator construction of complete worker origins."""

from __future__ import annotations

import importlib.metadata
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys
from types import ModuleType
from typing import Annotated, Optional
from pydantic import Field

import pytest
from bioimageflow.worker_origins import resolve_worker_tool_origin
from bioimageflow_core import (
    InstalledModuleOriginV1,
    ProcessingTool,
    SharedModuleOriginV1,
)
from bioimageflow_core.worker_origins import load_worker_tool


def test_same_id_source_archive_retains_its_actual_worker_body(tmp_path) -> None:
    from bioimageflow import Workflow
    from bioimageflow_core import Arguments

    fixture = json.loads(Path("tests/fixtures/unified_workflow_archive.json").read_text())
    graph = deepcopy(fixture["workflow"]["nodes"][0]["workflow"])
    source_id = "worker-collision-" + tmp_path.name
    graph["nodes"][0]["source_module"] = source_id

    def archive(value):
        source = f'''from bioimageflow_core import ProcessingTool, IOModel, EnvironmentSpec, RowConsumption
class CollisionTool(ProcessingTool):
    row_consumption = RowConsumption.MAPPED
    environment = EnvironmentSpec("collision", {{}})
    class Inputs(IOModel): pass
    class Outputs(IOModel): value: int
    def process_row(self, arguments):
        return self.Outputs(value={value})
'''
        return {"archive_version": 1, "workflow": deepcopy(graph), "custom_sources": [{
            "id": source_id, "module": "custom.first", "filename": "first.py",
            "source": source, "source_hash": hashlib.sha256(source.encode()).hexdigest(),
        }]}

    workflow_a = Workflow.from_dict(archive(1), storage_path=tmp_path / "records", engine="direct")
    tool_a = workflow_a.nodes["collision"].tool
    workflow_b = Workflow.from_dict(archive(9), storage_path=tmp_path / "records", engine="direct")
    tool_b = workflow_b.nodes["collision"].tool
    assert tool_a.process_row(Arguments()).value == 1
    assert tool_b.process_row(Arguments()).value == 9
    assert load_worker_tool(resolve_worker_tool_origin(tool_a)).process_row(Arguments()).value == 1
    assert load_worker_tool(resolve_worker_tool_origin(tool_b)).process_row(Arguments()).value == 9


def test_source_checkout_defaults_to_a_verified_shared_module() -> None:
    origin = resolve_worker_tool_origin(ProcessingTool)

    assert isinstance(origin, SharedModuleOriginV1)
    assert origin.module == "bioimageflow_core.tool"
    assert origin.class_name == "ProcessingTool"


def test_installed_module_requires_explicit_distribution_identity() -> None:
    try:
        version = importlib.metadata.version("bioimageflow-core")
    except importlib.metadata.PackageNotFoundError:
        pytest.skip("bioimageflow-core metadata is unavailable")

    origin = resolve_worker_tool_origin(
        ProcessingTool,
        installed_distribution="bioimageflow-core",
    )

    assert origin == InstalledModuleOriginV1(
        distribution="bioimageflow-core",
        version=version,
        module="bioimageflow_core.tool",
        class_name="ProcessingTool",
    )
    assert type(load_worker_tool(origin)) is ProcessingTool


def test_installed_distribution_spelling_must_be_canonical() -> None:
    with pytest.raises(ValueError, match="canonical normalized"):
        resolve_worker_tool_origin(
            ProcessingTool,
            installed_distribution="bioimageflow_core",
        )


@pytest.mark.parametrize("change", ["body", "literal_global", "helper"])
def test_managed_stale_source_refuses_before_cache_lookup(tmp_path, monkeypatch, change):
    from bioimageflow import Workflow
    from bioimageflow.engine import DefaultEngine
    from bioimageflow.storage import Storage

    def source(value):
        prefix = {"body": "", "literal_global": f"VALUE={value}\n", "helper": f"def helper(): return {value}\n"}[change]
        expression = {"body": str(value), "literal_global": "VALUE", "helper": "helper()"}[change]
        return f'''from bioimageflow_core import ProcessingTool, IOModel, EnvironmentSpec, RowConsumption
{prefix}
class Source(ProcessingTool):
    row_consumption = RowConsumption.MAPPED
    environment = EnvironmentSpec("stale", {{}})
    class Inputs(IOModel): pass
    class Outputs(IOModel): value: int
    def process_row(self, arguments): return self.Outputs(value={expression})
'''

    path = tmp_path / "tool.py"
    path.write_text(source(1))
    module = ModuleType("managed_stale_" + change)
    module.__file__ = str(path)
    monkeypatch.setitem(sys.modules, module.__name__, module)
    exec(compile(source(1), str(path), "exec", dont_inherit=True), module.__dict__)
    with Workflow(engine="direct", storage_path=tmp_path / "records") as workflow:
        node = module.Source()(name="stale")
    path.write_text(source(9))
    def forbidden_lookup(*args, **kwargs):
        raise AssertionError("stale executable reached cache lookup")
    monkeypatch.setattr(Storage, "load_current", forbidden_lookup)
    engine = DefaultEngine(use_wetlands=True, env_manager=object(), resource_lifetime="external")
    with pytest.raises(ValueError, match="Resident/source"):
        workflow.compute(node, engine=engine)


def test_captured_source_origin_refuses_later_disk_change(tmp_path, monkeypatch):
    from bioimageflow.cache.identity import deterministic_serialize
    from bioimageflow.worker_origins import capture_tool_executable

    path = tmp_path / "source.py"
    source = '''from bioimageflow_core import ProcessingTool, IOModel, EnvironmentSpec, RowConsumption
class Source(ProcessingTool):
    row_consumption = RowConsumption.MAPPED
    environment = EnvironmentSpec("source", {})
    class Inputs(IOModel): pass
    class Outputs(IOModel): value: int
    def process_row(self, arguments): return self.Outputs(value=1)
'''
    path.write_text(source)
    module = ModuleType("managed_capture")
    module.__file__ = str(path)
    monkeypatch.setitem(sys.modules, module.__name__, module)
    exec(compile(source, str(path), "exec", dont_inherit=True), module.__dict__)
    capture = capture_tool_executable(module.Source(), managed=True, canonicalize=deterministic_serialize)
    path.write_text(source.replace("value=1", "value=9"))
    with pytest.raises(ImportError, match="hash"):
        load_worker_tool(capture.worker_origin)


def test_direct_step_dispatch_uses_its_admitted_callback(tmp_path, monkeypatch):
    from bioimageflow import Workflow
    from bioimageflow_core import EnvironmentSpec, IOModel, RowConsumption

    class Source(ProcessingTool):
        row_consumption = RowConsumption.MAPPED
        environment = EnvironmentSpec("retained", {})
        class Inputs(IOModel):
            pass
        class Outputs(IOModel):
            value: int
        def process_row(self, arguments):
            return self.Outputs(value=4)

    with Workflow(engine="direct", storage_path=tmp_path / "records") as workflow:
        node = Source()(name="retained")
    steps = workflow.compute_steps(node)
    step = next(steps)
    assert not step.cached
    def replacement(self, arguments):
        return self.Outputs(value=99)
    monkeypatch.setattr(Source, "process_row", replacement)
    monkeypatch.setattr(Source, "process_batch", lambda self, arguments: self.Outputs(value=999))
    assert step.execute()["value"].tolist() == [4]
    steps.close()


def test_declared_installed_version_token_keeps_manual_cache_invalidation(tmp_path, monkeypatch):
    from bioimageflow import Workflow
    from bioimageflow_core import EnvironmentSpec, IOModel, RowConsumption

    class Source(ProcessingTool):
        row_consumption = RowConsumption.MAPPED
        environment = EnvironmentSpec("version-token", {})
        calls = []
        class Inputs(IOModel):
            pass
        class Outputs(IOModel):
            value: int
        def process_row(self, arguments):
            self.calls.append(4)
            return self.Outputs(value=4)

    version = "1.0"
    original = importlib.metadata.version
    def metadata_version(distribution):
        if distribution == "different-distribution-name":
            return version
        return original(distribution)
    monkeypatch.setattr(importlib.metadata, "version", metadata_version)
    monkeypatch.setattr(importlib.metadata, "packages_distributions", lambda: {
        Source.__module__.split(".", 1)[0]: ["different-distribution-name"],
    })
    with Workflow(engine="direct", storage_path=tmp_path / "records") as workflow:
        node = Source()(name="token")
    assert workflow.compute(node)["value"].tolist() == [4]
    assert workflow.compute(node)["value"].tolist() == [4]
    assert Source.calls == [4]
    version = "2.0"
    assert workflow.compute(node)["value"].tolist() == [4]
    assert Source.calls == [4, 4]
    assert workflow.compute(node)["value"].tolist() == [4]
    assert Source.calls == [4, 4]


def test_captured_output_contract_changes_key_and_actual_dtype(tmp_path, monkeypatch):
    import pandas as pd
    from bioimageflow import DataFrameTool, Workflow, WorkflowExecutionContext
    from bioimageflow_core import IOModel

    class Source(DataFrameTool):
        accepts_upstream = False
        _bif_package_version = "1.0"
        calls = []
        class Inputs(IOModel):
            pass
        class Outputs(IOModel):
            value: int
        def transform(self, df, arguments):
            annotation = self.Outputs._get_all_annotations()["value"]
            self.calls.append(annotation)
            return pd.DataFrame({"value": pd.Series([4], dtype=annotation)}, index=[0])

    with Workflow(engine="direct", storage_path=tmp_path / "records") as workflow:
        node = Source()(name="contract")
    first = WorkflowExecutionContext()
    assert workflow.compute(node, run_context=first)["value"].dtype == "int64"
    class FloatOutputs(IOModel):
        value: float
    monkeypatch.setattr(Source, "Outputs", FloatOutputs)
    second = WorkflowExecutionContext()
    assert workflow.compute(node, run_context=second)["value"].dtype == "float64"
    assert first.execution_outcomes[0].result_key != second.execution_outcomes[0].result_key
    assert Source.calls == [int, float]
    assert workflow.compute(node)["value"].dtype == "float64"
    assert Source.calls == [int, float]


def test_optional_wrapped_declared_bounds_change_admitted_key(tmp_path, monkeypatch):
    from bioimageflow import Workflow
    from bioimageflow_core import EnvironmentSpec, IOModel, RowConsumption

    class Source(ProcessingTool):
        row_consumption = RowConsumption.MAPPED
        environment = EnvironmentSpec("bounds", {})
        calls = []
        class Inputs(IOModel):
            value: Optional[Annotated[int, Field(gt=1)]] = 4
        class Outputs(IOModel):
            value: int
        def process_row(self, arguments):
            self.calls.append(arguments.value)
            return self.Outputs(value=arguments.value)

    with Workflow(engine="direct", storage_path=tmp_path / "records") as workflow:
        node = Source()(name="bounds")
    assert workflow.compute(node)["value"].tolist() == [4]
    class ChangedInputs(IOModel):
        value: Optional[Annotated[int, Field(gt=2)]] = 4
    monkeypatch.setattr(Source, "Inputs", ChangedInputs)
    assert workflow.compute(node)["value"].tolist() == [4]
    assert Source.calls == [4, 4]
    assert workflow.compute(node)["value"].tolist() == [4]
    assert Source.calls == [4, 4]
