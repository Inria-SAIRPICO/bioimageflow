"""Clean Workflow API contract tests."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import inspect

import pytest
import bioimageflow

from bioimageflow import OutputView, OutputViewCapability, Workflow
from bioimageflow.engine import DefaultEngine, SequentialEngine
from bioimageflow.node import get_active_workflow


PUBLIC_EXPORTS = {
    "BackendNotSupportedError",
    "BindingError",
    "CapabilityStatus",
    "ColumnNotFoundError",
    "ColumnRef",
    "CycleInWorkflowError",
    "DataFrameTool",
    "DefaultEngine",
    "DisabledNodeError",
    "DistributedExecutionPlan",
    "DistributedNodePlan",
    "ExecutionCapabilityReport",
    "ExecutorBinding",
    "ExecutorCapabilities",
    "IndexAlignmentError",
    "IntegrationDiagnostic",
    "InvalidatedSelection",
    "LauncherError",
    "LauncherProtocolError",
    "LauncherStateConflictError",
    "LocalUpload",
    "NodeFailureDiagnostic",
    "NodePlan",
    "NodePlanStatus",
    "NodeResourceOverrides",
    "NodeStep",
    "OutputView",
    "OutputViewCapability",
    "OrchestratorLaunchConfig",
    "PSIJLaunchConfig",
    "PSIJSubmissionUncertainError",
    "ParslConfigRef",
    "ParslConfigValidationReport",
    "ParslEngine",
    "ParslTaskError",
    "ParslTaskPolicy",
    "Passthrough",
    "PreparedRemoteSubmission",
    "PreparedSubmissionEntry",
    "PreparedSubmissionManifest",
    "ProgressEvent",
    "ResourceLifetime",
    "RemoteProfileValidationReport",
    "RemoteProfileDiagnostic",
    "RemoteWorkflowRun",
    "SSHSubmissionTransport",
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
    "WorkflowRun",
    "WorkflowRunFailedError",
    "WorkflowRunLostError",
    "WorkflowRunNotReadyError",
    "WorkflowRunResultUnavailableError",
    "WorkflowSession",
    "check_type_compat",
    "configure_logging",
    "configure_wetlands",
    "deserialize_constant",
    "effective_node_resources",
    "export_outputs",
    "get_execution_capabilities",
    "get_home",
    "get_inputs_schema",
    "get_tool_package_info",
    "get_tool_store_path",
    "get_wetlands_path",
    "load_versioned_package",
    "plan_distributed_execution",
    "prepare_remote_submission",
    "require_tool_packages",
    "serialize_constant",
    "serialize_image_spec",
    "serialize_input_schema",
    "serialize_output_schema",
    "serialize_resolved_outputs",
    "serialize_tool_metadata",
    "submit_workflow",
    "topological_order",
    "unload_versioned_package",
    "validate_parameters",
    "validate_parsl_config_ref",
    "validate_remote_execution_profile",
}


def test_public_exports_match_explicit_allowlist() -> None:
    assert set(bioimageflow.__all__) == PUBLIC_EXPORTS


def test_executable_workflow_apis_require_storage_path() -> None:
    callables = (
        Workflow,
        Workflow.from_dict,
        Workflow.load,
        Workflow.import_archive,
        Workflow.from_python,
        bioimageflow.WorkflowSession,
        bioimageflow.WorkflowSession.from_dict,
    )

    for callable_ in callables:
        parameter = inspect.signature(callable_).parameters["storage_path"]
        assert parameter.default is inspect.Parameter.empty


def test_workflow_defaults_to_wetlands_parallel_engine(tmp_path) -> None:
    wf = Workflow(storage_path=tmp_path)
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
    tmp_path,
) -> None:
    with pytest.raises(TypeError, match=message):
        Workflow(storage_path=tmp_path, **kwargs)


def test_workflow_builds_direct_parallel_engine(tmp_path) -> None:
    engine = Workflow(
        storage_path=tmp_path,
        engine="direct",
        execution="parallel",
    ).create_engine()

    assert isinstance(engine, DefaultEngine)
    assert not isinstance(engine, SequentialEngine)
    assert engine._use_wetlands is False
    assert engine._force_sequential is False


def test_workflow_builds_wetlands_sequential_engine(tmp_path) -> None:
    engine = Workflow(
        storage_path=tmp_path,
        execution="sequential",
    ).create_engine()

    assert isinstance(engine, SequentialEngine)
    assert engine._use_wetlands is True
    assert engine._force_sequential is True


def test_workflow_accepts_parsl_and_rejects_unknown_values(tmp_path) -> None:
    assert Workflow(storage_path=tmp_path, engine="parsl").engine_type == "parsl"

    with pytest.raises(ValueError, match="engine"):
        Workflow(storage_path=tmp_path, engine="unknown")

    with pytest.raises(ValueError, match="execution"):
        Workflow(storage_path=tmp_path, execution="serial")


def test_workflow_to_dict_uses_clean_config(tmp_path) -> None:
    wf = Workflow(storage_path=tmp_path, engine="direct", execution="sequential")

    config = wf.to_dict()["config"]

    assert config["engine"] == "direct"
    assert config["execution"] == "sequential"
    assert "storage_path" not in config
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

    graph = Workflow(
        storage_path=tmp_path,
        output_view={"mode": "copy", "scope": "both"},
    ).to_dict()
    graph["config"] = config
    loaded = Workflow.from_dict(graph, storage_path=tmp_path)

    assert loaded.output_view == OutputView(mode="copy", scope="both")


def test_workflow_output_view_string_shorthand(tmp_path) -> None:
    wf = Workflow(storage_path=tmp_path, output_view="symlink")

    assert wf.output_view == OutputView(mode="symlink", scope="latest")


def test_workflow_pointer_output_view_round_trips(tmp_path) -> None:
    wf = Workflow(
        storage_path=tmp_path,
        engine="direct",
        output_view={"mode": "pointer", "scope": "both"},
    )

    graph = wf.to_dict()
    loaded = Workflow.from_dict(graph, storage_path=tmp_path)

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
def test_workflow_rejects_invalid_output_view(
    output_view: dict[str, str],
    tmp_path,
) -> None:
    with pytest.raises(ValueError, match="output_view"):
        Workflow(storage_path=tmp_path, output_view=output_view)


def test_workflow_from_dict_defaults_to_wetlands_engine(tmp_path) -> None:
    graph = Workflow(storage_path=tmp_path).to_dict()
    graph["config"] = {}
    wf = Workflow.from_dict(graph, storage_path=tmp_path)

    assert wf.engine_type == "wetlands"
    assert wf.execution == "parallel"


def test_active_workflow_is_context_local_across_threads(tmp_path) -> None:
    def active_inside_context() -> Workflow:
        with Workflow(storage_path=tmp_path) as wf:
            assert get_active_workflow() is wf
            return wf

    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = pool.map(lambda _: active_inside_context(), range(2))

    assert first is not second
    assert get_active_workflow() is None
