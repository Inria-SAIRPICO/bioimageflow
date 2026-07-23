"""Clean Workflow API contract tests."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest
import bioimageflow

from bioimageflow import OutputView, OutputViewCapability, Workflow
from bioimageflow.engine import DefaultEngine, SequentialEngine
from bioimageflow.node import get_active_workflow


PUBLIC_EXPORTS = {
    "BindingError",
    "ColumnNotFoundError",
    "ColumnRef",
    "CycleInWorkflowError",
    "DataFrameTool",
    "DefaultEngine",
    "DisabledNodeError",
    "ExecutorBinding",
    "ExecutorCapabilities",
    "IndexAlignmentError",
    "InvalidatedSelection",
    "NodePlan",
    "NodePlanStatus",
    "NodeStep",
    "OutputView",
    "OutputViewCapability",
    "ParslEngine",
    "ParslTaskError",
    "ParslTaskPolicy",
    "Passthrough",
    "ProgressEvent",
    "ResourceLifetime",
    "SchemaSerializationError",
    "SequentialEngine",
    "SourceToolUpstreamError",
    "ToolMetadata",
    "ToolRegistry",
    "ValidationError",
    "ValidationErrorKind",
    "WetlandsEnvManager",
    "WorkerEnvironmentAttestation",
    "WorkerSlotCapacity",
    "WorkerTaskError",
    "WorkerTimeoutError",
    "Workflow",
    "WorkflowCancelledError",
    "WorkflowExecutionContext",
    "WorkflowNode",
    "WorkflowSession",
    "check_type_compat",
    "configure_logging",
    "configure_wetlands",
    "deserialize_constant",
    "get_home",
    "get_inputs_schema",
    "get_tool_package_info",
    "get_tool_store_path",
    "get_wetlands_path",
    "load_versioned_package",
    "require_tool_packages",
    "serialize_constant",
    "serialize_image_spec",
    "serialize_input_schema",
    "serialize_output_schema",
    "serialize_resolved_outputs",
    "serialize_tool_metadata",
    "topological_order",
    "unload_versioned_package",
    "validate_parameters",
}


def test_public_exports_match_explicit_allowlist() -> None:
    assert set(bioimageflow.__all__) == PUBLIC_EXPORTS


def test_workflow_defaults_to_wetlands_parallel_engine() -> None:
    wf = Workflow()
    engine = wf.create_engine()

    assert wf.engine_type == "wetlands"
    assert wf.execution == "parallel"
    assert isinstance(engine, DefaultEngine)
    assert not isinstance(engine, SequentialEngine)
    assert engine._use_wetlands is True
    assert engine._force_sequential is False
    assert not hasattr(wf, "use_wetlands")
    assert not hasattr(wf, "max_age")
    assert not hasattr(wf, "max_executions")


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"use_wetlands": False}, "use_wetlands"),
        ({"max_age": "7d"}, "max_age"),
        ({"max_executions": 3}, "max_executions"),
    ],
)
def test_workflow_rejects_removed_constructor_arguments(
    kwargs: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(TypeError, match=message):
        Workflow(**kwargs)


def test_workflow_builds_direct_parallel_engine() -> None:
    engine = Workflow(engine="direct", execution="parallel").create_engine()

    assert isinstance(engine, DefaultEngine)
    assert not isinstance(engine, SequentialEngine)
    assert engine._use_wetlands is False
    assert engine._force_sequential is False


def test_workflow_builds_wetlands_sequential_engine() -> None:
    engine = Workflow(execution="sequential").create_engine()

    assert isinstance(engine, SequentialEngine)
    assert engine._use_wetlands is True
    assert engine._force_sequential is True


def test_workflow_accepts_parsl_and_rejects_unknown_values() -> None:
    assert Workflow(engine="parsl").engine_type == "parsl"

    with pytest.raises(ValueError, match="engine"):
        Workflow(engine="unknown")

    with pytest.raises(ValueError, match="execution"):
        Workflow(execution="serial")


def test_workflow_to_dict_uses_clean_config(tmp_path) -> None:
    wf = Workflow(storage_path=tmp_path, engine="direct", execution="sequential")

    config = wf.to_dict()["config"]

    assert config["engine"] == "direct"
    assert config["execution"] == "sequential"
    assert "use_wetlands" not in config
    assert "max_age" not in config
    assert "max_executions" not in config


def test_workflow_output_view_normalizes_and_round_trips(tmp_path) -> None:
    wf = Workflow(
        storage_path=tmp_path,
        engine="direct",
        output_view={"mode": "copy", "scope": "both"},
    )

    assert wf.output_view == OutputView(mode="copy", scope="both")
    config = wf.to_dict()["config"]
    assert config["output_view"] == {"mode": "copy", "scope": "both"}

    graph = Workflow(output_view={"mode": "copy", "scope": "both"}).to_dict()
    graph["config"] = config
    loaded = Workflow.from_dict(graph)

    assert loaded.output_view == OutputView(mode="copy", scope="both")


def test_workflow_output_view_string_shorthand() -> None:
    wf = Workflow(output_view="symlink")

    assert wf.output_view == OutputView(mode="symlink", scope="latest")


def test_workflow_pointer_output_view_round_trips(tmp_path) -> None:
    wf = Workflow(
        storage_path=tmp_path,
        engine="direct",
        output_view={"mode": "pointer", "scope": "both"},
    )

    graph = wf.to_dict()
    loaded = Workflow.from_dict(graph)

    assert loaded.output_view == OutputView(mode="pointer", scope="both")
    assert graph["config"]["output_view"] == {"mode": "pointer", "scope": "both"}
    assert (
        OutputViewCapability(mode="pointer", supported=True, code="ok").supported
        is True
    )


@pytest.mark.parametrize(
    "output_view",
    [
        {"mode": "invalid", "scope": "latest"},
        {"mode": "copy", "scope": "invalid"},
    ],
)
def test_workflow_rejects_invalid_output_view(output_view: dict[str, str]) -> None:
    with pytest.raises(ValueError, match="output_view"):
        Workflow(output_view=output_view)


def test_workflow_from_dict_defaults_to_wetlands_engine() -> None:
    graph = Workflow().to_dict()
    graph["config"] = {}
    wf = Workflow.from_dict(graph)

    assert wf.engine_type == "wetlands"
    assert wf.execution == "parallel"


def test_active_workflow_is_context_local_across_threads() -> None:
    def active_inside_context() -> Workflow:
        with Workflow() as wf:
            assert get_active_workflow() is wf
            return wf

    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = pool.map(lambda _: active_inside_context(), range(2))

    assert first is not second
    assert get_active_workflow() is None
