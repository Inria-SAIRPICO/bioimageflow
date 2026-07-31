from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path, PurePosixPath
from types import SimpleNamespace

import pytest

import bioimageflow.launcher.ssh as ssh_module
import bioimageflow.launcher.configuration as configuration_module
import bioimageflow.launcher.psij as psij_module

from bioimageflow import (
    DistributedExecutionPlan,
    ExecutionCapabilityReport,
    ExecutorBinding,
    ExecutorCapabilities,
    LocalUpload,
    NodeResourceOverrides,
    PreparedSubmissionManifest,
    RemoteProfileDiagnostic,
    RemoteProfileValidationReport,
    SSHSubmissionTransport,
    PSIJLaunchConfig,
    ParslConfigRef,
    ParslTaskPolicy,
    WorkerEnvironmentAttestation,
    WorkerSlotCapacity,
    Workflow,
    get_execution_capabilities,
    plan_distributed_execution,
    prepare_remote_submission,
    validate_parsl_config_ref,
    validate_remote_execution_profile,
)
from bioimageflow.cache import compute_env_hash
from bioimageflow.backends import DirectBackend
from bioimageflow.engine import DefaultEngine
from bioimageflow.parsl.startup import CORE_REQUIREMENT
from bioimageflow.parsl.startup import prepare_parsl_execution
from bioimageflow.launcher.profile_validation import validate_profile_on_cluster
from bioimageflow_core import Arguments, EnvironmentSpec, IOModel, ProcessingTool, ResourceSpec


TEST_ENV = EnvironmentSpec(
    name="contract-test",
    dependencies={"python": "3.12", "pip": ["numpy==2.5.0"]},
)


class ContractTool(ProcessingTool):
    environment = TEST_ENV
    resources = ResourceSpec(cpu=2, gpu=0, memory="2GB", max_concurrent=8)

    class Inputs(IOModel):
        value: int = 1

    class Outputs(IOModel):
        value: int

    def process_row(self, arguments: Arguments):
        return self.Outputs(value=arguments.value)


class FailingContractTool(ContractTool):
    def process_row(self, arguments: Arguments):
        del arguments
        raise RuntimeError("independent failure")


def _binding(label: str = "threads") -> ExecutorBinding:
    return ExecutorBinding(
        label=label,
        environments=(
            WorkerEnvironmentAttestation(
                name=TEST_ENV.name,
                dependency_hash=compute_env_hash(TEST_ENV.dependencies),
                allow_flexible_versions=False,
                core_requirement=CORE_REQUIREMENT,
            ),
        ),
        capabilities=ExecutorCapabilities(
            storage_modes=("shared_fs",),
            tool_origin_modes=("shared_module", "source_file", "installed_module"),
            slot=WorkerSlotCapacity(cpu=8, memory_bytes=16 * 1024**3),
        ),
    )


def test_node_overrides_are_instance_specific_and_round_trip(tmp_path: Path) -> None:
    workflow = Workflow(storage_path=tmp_path / "source", engine="direct")
    with workflow:
        first = ContractTool()(name="first")
        second = ContractTool()(name="second")
    first.set_resource_overrides(
        NodeResourceOverrides(cpu=4, memory="4GB", max_concurrent=2)
    )
    second.set_resource_overrides(NodeResourceOverrides(cpu=6))

    payload = workflow.to_dict()
    restored = Workflow.from_dict(payload, storage_path=tmp_path / "restored")

    assert restored.nodes["first"].resource_overrides == first.resource_overrides
    assert restored.nodes["second"].resource_overrides == second.resource_overrides
    assert restored.nodes["first"].effective_resources.cpu == 4
    assert restored.nodes["first"].effective_resources.max_concurrent == 2
    assert restored.nodes["second"].effective_resources.cpu == 6
    assert ContractTool.resources.cpu == 2


def test_recursive_node_overrides_round_trip(tmp_path: Path) -> None:
    child = Workflow(
        name="child",
        storage_path=tmp_path / "child-results",
        engine="direct",
    )
    with child:
        source = ContractTool()(name="source")
        source.set_resource_overrides(NodeResourceOverrides(cpu=3))
        child.output("value", source["value"], id="child-value")

    parent = Workflow(storage_path=tmp_path / "parent-results", engine="direct")
    with parent:
        child(name="nested")

    restored = Workflow.from_dict(
        parent.to_dict(),
        storage_path=tmp_path / "restored",
    )
    nested = restored.nodes["nested"]

    assert nested.workflow.nodes["source"].resource_overrides.cpu == 3


def test_resource_floors_and_concurrency_caps_are_validated() -> None:
    assert NodeResourceOverrides(cpu=4, max_concurrent=2).effective(
        ContractTool.resources
    ).max_concurrent == 2
    for value in (
        NodeResourceOverrides(cpu=1),
        NodeResourceOverrides(memory="1GB"),
        NodeResourceOverrides(max_concurrent=9),
        NodeResourceOverrides(max_concurrent=0),
    ):
        try:
            value.effective(ContractTool.resources)
        except ValueError:
            pass
        else:
            raise AssertionError("invalid override was accepted")


def test_planning_is_non_allocating_and_uses_effective_resources(tmp_path: Path) -> None:
    workflow = Workflow(storage_path=tmp_path / "results", engine="direct")
    with workflow:
        node = ContractTool()(name="worker")
    node.set_resource_overrides(NodeResourceOverrides(cpu=4, max_concurrent=3))
    task_policy = ParslTaskPolicy(row_chunk_size=2, max_in_flight=7)

    plan = plan_distributed_execution(
        workflow,
        executor_bindings={"threads": _binding()},
        task_policy=task_policy,
    )

    assert plan.allocates_resources is False
    assert plan.valid
    assert len(plan.nodes) == 1
    assert plan.nodes[0].resources.cpu == 4
    assert plan.nodes[0].resources.max_concurrent == 3
    assert plan.nodes[0].selected_executor == "threads"
    assert plan.nodes[0].environment_name == TEST_ENV.name
    assert plan.nodes[0].environment_identity is not None
    assert plan.nodes[0].execution_status == "unexecuted"
    assert plan.nodes[0].will_dispatch
    assert plan.task_policy == task_policy
    assert not (tmp_path / "results" / "runs").exists()
    assert DistributedExecutionPlan.from_dict(plan.to_dict()) == plan


def test_public_plan_does_not_route_cached_processing_nodes(tmp_path: Path) -> None:
    workflow = Workflow(storage_path=tmp_path / "results", engine="direct")
    with workflow:
        node = ContractTool()(name="worker")
    workflow.compute(node)

    plan = plan_distributed_execution(
        workflow,
        executor_bindings={
            "first": _binding("first"),
            "second": _binding("second"),
        },
    )

    assert plan.valid
    assert plan.nodes[0].execution_status == "cached"
    assert not plan.nodes[0].will_dispatch
    assert plan.nodes[0].selected_executor is None
    assert plan.nodes[0].route_reason == "cached: no worker dispatch"


def test_public_plan_and_runtime_startup_select_the_same_route(
    tmp_path: Path,
) -> None:
    workflow = Workflow(storage_path=tmp_path / "results", engine="direct")
    with workflow:
        node = ContractTool()(name="worker")
    node.set_resource_overrides(NodeResourceOverrides(cpu=4))
    bindings = {"threads": _binding()}

    public = plan_distributed_execution(
        workflow,
        executor_bindings=bindings,
    )
    runtime = prepare_parsl_execution(
        [node],
        workflow,
        executor_bindings=bindings,
        node_routes={},
        environment_routes={},
        shared_runtime_root=None,
        storage_mode="shared_fs",
        sequential=False,
        cancellation_requested=lambda: False,
    )

    assert public.nodes[0].selected_executor == runtime.routing.routes[0].executor_label
    assert public.nodes[0].resources == runtime.routing.routes[0].requirement.resources


def test_backend_dispatch_receives_effective_node_resources(
    tmp_path: Path,
) -> None:
    captured = []

    class CapturingBackend(DirectBackend):
        def dispatch(self, engine, request):
            captured.append(request.resources)
            return super().dispatch(engine, request)

    workflow = Workflow(storage_path=tmp_path / "results", engine="direct")
    with workflow:
        node = ContractTool()(name="worker")
    node.set_resource_overrides(
        NodeResourceOverrides(
            cpu=4,
            gpu=1,
            memory="4GB",
            gpu_memory="2GB",
            max_concurrent=3,
        )
    )
    engine = DefaultEngine(use_wetlands=False)
    engine._backend = CapturingBackend()

    workflow.compute(node, engine=engine)

    assert captured == [node.effective_resources]


def test_attached_callbacks_keep_parallel_failures_independent(
    tmp_path: Path,
) -> None:
    events = []
    workflow = Workflow(
        storage_path=tmp_path / "results",
        engine="direct",
        on_progress=events.append,
    )
    with workflow:
        first = FailingContractTool()(name="first")
        second = FailingContractTool()(name="second")

    try:
        workflow.compute(first, second)
    except RuntimeError:
        pass
    else:
        raise AssertionError("parallel failures were accepted")

    diagnostics = {
        event.diagnostic.scoped_node_path: event.diagnostic
        for event in events
        if event.status == "failed" and event.diagnostic is not None
    }
    assert set(diagnostics) == {"first", "second"}
    assert all(item.terminal for item in diagnostics.values())


def test_config_validation_is_sanitized_and_checks_labels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report = validate_parsl_config_ref(
        ParslConfigRef(
            "tests.unit.launcher.config_factories:build_threads",
            {"max_threads": 1},
        ),
        executor_bindings={"threads": _binding()},
        trusted_factories={
            "tests.unit.launcher.config_factories:build_threads"
        },
    )

    assert report.valid
    assert report.executor_labels == ("threads",)
    missing = validate_parsl_config_ref(
        ParslConfigRef(
            "tests.unit.launcher.config_factories:build_threads",
            {},
            {"credential": "BIOIMAGEFLOW_TEST_MISSING_SECRET"},
        ),
        executor_bindings={"threads": _binding()},
        trusted_factories={
            "tests.unit.launcher.config_factories:build_threads"
        },
    )
    assert not missing.valid
    assert missing.diagnostics[0].code == "missing-secret-reference"
    assert "credential" not in missing.diagnostics[0].message

    monkeypatch.setenv("BIF_VALUE", "do-not-expose-this")
    rejected = validate_parsl_config_ref(
        ParslConfigRef(
            "tests.unit.launcher.config_factories:fail_with_credential",
            {},
            {"credential": "BIF_VALUE"},
        ),
        executor_bindings={"threads": _binding()},
        trusted_factories={
            "tests.unit.launcher.config_factories:fail_with_credential"
        },
    )
    assert "do-not-expose-this" not in rejected.diagnostics[0].message


def test_capability_discovery_has_no_live_runtime_values() -> None:
    payload = get_execution_capabilities().to_dict()

    assert payload["capabilities"]["direct"]["supported"]
    assert payload["capabilities"]["portable_resource_overrides"]["supported"]
    json.dumps(payload)
    assert ExecutionCapabilityReport.from_dict(payload).to_dict() == payload


def test_remote_preparation_binds_copied_upload_bytes(tmp_path: Path) -> None:
    source = tmp_path / "input.bin"
    source.write_bytes(b"validated")
    workflow = Workflow(storage_path=Path("/cluster/results"))
    with workflow:
        workflow.input("source", Path, id="source")

    prepared = prepare_remote_submission(
        workflow,
        inputs={"source": LocalUpload(source)},
        targets=None,
        parsl_config=ParslConfigRef(
            "tests.unit.launcher.config_factories:build_threads",
            {},
        ),
        executor_bindings={"threads": _binding()},
        launch=PSIJLaunchConfig(
            executor="slurm",
            walltime=timedelta(minutes=5),
        ),
    )
    try:
        digest = prepared.manifest.bundle_digest
        manifest = PreparedSubmissionManifest.from_dict(
            prepared.manifest.to_dict()
        )
        source.write_bytes(b"changed")
        assert prepared.manifest.bundle_digest == digest
        assert manifest == prepared.manifest
        assert all(entry.path for entry in manifest.entries)
        assert not prepared.expired
    finally:
        prepared.close()
    assert prepared.closed


def test_remote_profile_validation_uses_non_submitting_operation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = []

    def execute(transport, operation, arguments, *, request_id):
        del transport, request_id
        calls.append((operation, arguments))
        return RemoteProfileValidationReport(
            valid=True,
            diagnostics=(),
            executor_labels=("threads",),
        ).to_dict()

    monkeypatch.setattr(ssh_module, "execute_cluster_command", execute)
    transport = SSHSubmissionTransport(
        host="cluster",
        staging_root=PurePosixPath("/cluster/staging"),
        remote_executable=PurePosixPath("/cluster/bin/bioimageflow-cluster-agent"),
    )
    report = validate_remote_execution_profile(
        transport=transport,
        parsl_config=ParslConfigRef(
            "tests.unit.launcher.config_factories:build_threads",
            {},
        ),
        executor_bindings={"threads": _binding()},
        launch=PSIJLaunchConfig(
            executor="slurm",
            walltime=timedelta(minutes=5),
        ),
        storage_path="/cluster/results",
    )

    assert report.valid
    assert calls[0][0] == "validate-profile"
    assert calls[0][1]["storage_path"] == "/cluster/results"
    failure = RemoteProfileValidationReport(
        valid=False,
        diagnostics=(
            RemoteProfileDiagnostic("missing-secret-reference", "Unavailable."),
        ),
    )
    assert RemoteProfileValidationReport.from_dict(failure.to_dict()) == failure


def test_cluster_profile_validation_does_not_create_run_or_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = []

    def build(reference, *, trusted_factories):
        calls.append((reference.factory, tuple(trusted_factories)))
        return SimpleNamespace(
            retries=0,
            executors=[SimpleNamespace(label="threads")],
        )

    runtime = SimpleNamespace(
        JobExecutor=SimpleNamespace(
            get_executor_names=lambda: {"slurm", "pbs", "lsf"}
        )
    )
    monkeypatch.setattr(configuration_module, "build_parsl_config", build)
    monkeypatch.setattr(psij_module, "_load_runtime", lambda: runtime)

    payload = validate_profile_on_cluster(
        {
            "parsl_config": ParslConfigRef(
                "tests.unit.launcher.config_factories:build_threads",
                {},
            ).to_dict(),
            "executor_bindings": {"threads": _binding().to_dict()},
            "launch": PSIJLaunchConfig(
                executor="slurm",
                walltime=timedelta(minutes=5),
            ).to_dict(),
            "storage_path": "/cluster/results",
            "staging_root": "/cluster/staging",
        }
    )
    report = RemoteProfileValidationReport.from_dict(payload)

    assert report.valid
    assert not report.allocation_created
    assert not report.workflow_run_created
    assert calls == [
        (
            "tests.unit.launcher.config_factories:build_threads",
            ("tests.unit.launcher.config_factories:build_threads",),
        )
    ]
