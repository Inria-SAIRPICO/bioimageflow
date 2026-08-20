"""Strict validation of executor preflight evidence."""

from __future__ import annotations

from copy import deepcopy
import hashlib
import importlib.metadata
from pathlib import Path

import pytest

from bioimageflow.parsl.preflight import (
    ExecutorPreflightResultV1,
    ParslPreflightError,
    PreflightExpectation,
    WORKER_API,
    build_preflight_expectation,
    build_preflight_payload,
    validate_preflight_result,
    validate_preflight_results,
)
from bioimageflow.parsl.requirements import (
    NormalizedResourceRequest,
    WorkerRequirement,
)
from bioimageflow.parsl.routing import resolve_executor_routes
from bioimageflow.parsl.startup import CORE_REQUIREMENT as CURRENT_CORE_REQUIREMENT
from bioimageflow.parsl.types import (
    ExecutorBinding,
    ExecutorCapabilities,
    WorkerEnvironmentAttestation,
    WorkerSlotCapacity,
)
from bioimageflow_core.worker_origins import (
    InstalledModuleOriginV1,
    SourceFileOriginV1,
)
from bioimageflow_core.preflight import execute_executor_preflight


CORE_REQUIREMENT = "bioimageflow-core>=0.1.7,<0.2"


def _binding() -> ExecutorBinding:
    return ExecutorBinding(
        label="cpu",
        environments=(
            WorkerEnvironmentAttestation(
                name="analysis",
                dependency_hash="a" * 64,
                allow_flexible_versions=False,
                core_requirement=CORE_REQUIREMENT,
            ),
        ),
        capabilities=ExecutorCapabilities(
            storage_modes=("shared_fs",),
            tool_origin_modes=("installed_module", "source_file"),
            slot=WorkerSlotCapacity(cpu=4),
        ),
    )


def _requirement(
    node: str,
    origin,
    *,
    dependency_path: str,
) -> WorkerRequirement:
    return WorkerRequirement(
        scoped_node_name=node,
        environment_name="analysis",
        dependency_hash="a" * 64,
        allow_flexible_versions=False,
        core_requirement=CORE_REQUIREMENT,
        anchored_dependency_paths=(dependency_path,),
        resources=NormalizedResourceRequest(),
        tool_origin=origin,
    )


def _expectation(tmp_path: Path) -> PreflightExpectation:
    source = tmp_path / "tools.py"
    source.write_text("class Tool: pass\n")
    dependency = tmp_path / "environment"
    dependency.mkdir()
    installed = InstalledModuleOriginV1(
        distribution="example-tools",
        version="1.0.0",
        module="example_tools.worker",
        class_name="InstalledTool",
    )
    source_origin = SourceFileOriginV1(
        path=str(source.resolve()),
        source_hash="b" * 64,
        class_name="SourceTool",
    )
    requirements = [
        _requirement(
            "workflow/installed",
            installed,
            dependency_path=str(dependency.resolve()),
        ),
        _requirement(
            "workflow/source",
            source_origin,
            dependency_path=str(dependency.resolve()),
        ),
    ]
    plan = resolve_executor_routes(
        requirements,
        executor_bindings={"cpu": _binding()},
    )
    storage = tmp_path / "storage"
    storage.mkdir()
    return build_preflight_expectation(
        plan,
        "cpu",
        storage_root=storage.resolve(),
        sentinel_path=storage.resolve() / ".preflight" / "session" / "cpu",
        additional_readable_paths=(tmp_path.resolve(),),
        expected_core_version="0.1.8",
    )


def _valid_payload(expectation: PreflightExpectation) -> dict[str, object]:
    return {
        "schema": ExecutorPreflightResultV1.SCHEMA,
        "executor_label": expectation.executor_label,
        "worker_api": WORKER_API,
        "core_version": "0.1.8",
        "core_requirements": list(expectation.core_requirements),
        "core_compatible": True,
        "storage_root": expectation.storage_root,
        "sentinel_path": expectation.sentinel_path,
        "sentinel_write": True,
        "sentinel_read": True,
        "sentinel_delete": True,
        "path_results": [
            {
                "path": path,
                "resolved_path": path,
                "readable": True,
            }
            for path in expectation.readable_paths
        ],
        "origin_results": [
            {
                "identity": identity,
                "kind": kind,
                "resolved": True,
            }
            for identity, kind in sorted(expectation.origin_identities.items())
        ],
    }


def test_validates_complete_success_evidence(tmp_path: Path) -> None:
    expectation = _expectation(tmp_path)

    result = validate_preflight_result(
        _valid_payload(expectation),
        expectation,
    )

    assert result.executor_label == "cpu"
    assert result.core_version == "0.1.8"
    assert len(result.origin_results) == 2
    assert set(path.path for path in result.path_results) == set(
        expectation.readable_paths
    )
    assert ExecutorPreflightResultV1.from_dict(result.to_dict()) == result


def test_worker_probe_payload_round_trips_through_result_validation(
    tmp_path: Path,
) -> None:
    source = tmp_path / "probe_tool.py"
    source.write_text(
        """
from bioimageflow_core import Arguments, IOModel, ProcessingTool, RowConsumption

class ProbeTool(ProcessingTool):
    row_consumption = RowConsumption.MAPPED
    class Inputs(IOModel):
        value: str
    class Outputs(IOModel):
        value: str
    def process_row(self, arguments: Arguments):
        return self.Outputs(value=arguments.value)
""",
        encoding="utf-8",
    )
    source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    storage = tmp_path / "storage"
    storage.mkdir()
    origin = SourceFileOriginV1(
        path=str(source.resolve()),
        source_hash=source_hash,
        class_name="ProbeTool",
    )
    expectation = PreflightExpectation(
        executor_label="cpu",
        environment_identities=("env_test",),
        core_requirements=(CURRENT_CORE_REQUIREMENT,),
        storage_root=str(storage.resolve()),
        sentinel_path=str(
            (storage / ".preflight" / "session" / "sentinel").resolve()
        ),
        readable_paths=(
            str(source.resolve()),
            str(storage.resolve()),
        ),
        origins=(origin,),
        expected_core_version=importlib.metadata.version("bioimageflow-core"),
    )

    result = validate_preflight_result(
        execute_executor_preflight(build_preflight_payload(expectation)),
        expectation,
    )

    assert result.executor_label == "cpu"
    assert result.origin_results[0].resolved is True
    assert not Path(expectation.sentinel_path).exists()


@pytest.mark.parametrize(
    ("field", "value", "evidence"),
    [
        ("executor_label", "other", "ran on executor"),
        ("worker_api", "bioimageflow.parsl.worker.v2", "worker API"),
        (
            "core_requirements",
            ["bioimageflow-core>=0.2,<0.3"],
            "checked core requirements",
        ),
        ("core_compatible", False, "incompatible bioimageflow-core"),
        ("core_version", "0.1.7", "reported core version"),
        ("storage_root", "/different/storage", "observed storage root"),
        ("sentinel_path", "/different/sentinel", "observed sentinel"),
        ("sentinel_write", False, "sentinel capabilities"),
        ("sentinel_read", False, "sentinel capabilities"),
        ("sentinel_delete", False, "sentinel capabilities"),
    ],
)
def test_rejects_mismatched_executor_core_and_storage_evidence(
    tmp_path: Path,
    field: str,
    value: object,
    evidence: str,
) -> None:
    expectation = _expectation(tmp_path)
    payload = {**_valid_payload(expectation), field: value}

    with pytest.raises(ParslPreflightError, match=evidence) as raised:
        validate_preflight_result(payload, expectation)

    assert "'cpu'" in str(raised.value)
    assert expectation.environment_identities[0] in str(raised.value)


def test_rejects_missing_extra_unreadable_and_remapped_paths(
    tmp_path: Path,
) -> None:
    expectation = _expectation(tmp_path)

    missing = _valid_payload(expectation)
    path_results = missing["path_results"]
    assert isinstance(path_results, list)
    path_results.pop()
    with pytest.raises(ParslPreflightError, match="returned shared paths"):
        validate_preflight_result(missing, expectation)

    extra = _valid_payload(expectation)
    extra_results = extra["path_results"]
    assert isinstance(extra_results, list)
    extra_results.append(
        {"path": "/extra", "resolved_path": "/extra", "readable": True}
    )
    with pytest.raises(ParslPreflightError, match="returned shared paths"):
        validate_preflight_result(extra, expectation)

    unreadable = _valid_payload(expectation)
    unreadable_results = unreadable["path_results"]
    assert isinstance(unreadable_results, list)
    unreadable_results[0]["readable"] = False
    with pytest.raises(ParslPreflightError, match="cannot read shared path"):
        validate_preflight_result(unreadable, expectation)

    remapped = _valid_payload(expectation)
    remapped_results = remapped["path_results"]
    assert isinstance(remapped_results, list)
    remapped_results[0]["resolved_path"] = "/worker/remapped"
    with pytest.raises(ParslPreflightError, match="resolves shared path"):
        validate_preflight_result(remapped, expectation)


def test_rejects_missing_wrong_kind_and_unresolved_origins(
    tmp_path: Path,
) -> None:
    expectation = _expectation(tmp_path)

    missing = _valid_payload(expectation)
    origin_results = missing["origin_results"]
    assert isinstance(origin_results, list)
    origin_results.pop()
    with pytest.raises(ParslPreflightError, match="origin identities"):
        validate_preflight_result(missing, expectation)

    wrong_kind = _valid_payload(expectation)
    wrong_kind_results = wrong_kind["origin_results"]
    assert isinstance(wrong_kind_results, list)
    wrong_kind_results[0]["kind"] = "archive_module"
    with pytest.raises(ParslPreflightError, match="failed .* origin"):
        validate_preflight_result(wrong_kind, expectation)

    unresolved = _valid_payload(expectation)
    unresolved_results = unresolved["origin_results"]
    assert isinstance(unresolved_results, list)
    unresolved_results[0]["resolved"] = False
    with pytest.raises(ParslPreflightError, match="resolved=False"):
        validate_preflight_result(unresolved, expectation)


def test_decoder_rejects_unknown_schema_fields_types_and_duplicates(
    tmp_path: Path,
) -> None:
    expectation = _expectation(tmp_path)
    payload = _valid_payload(expectation)

    unknown = {**payload, "schema": "bioimageflow.parsl.preflight.v2"}
    with pytest.raises(ParslPreflightError, match="Unknown"):
        validate_preflight_result(unknown, expectation)

    extra = {**payload, "secret": "do-not-accept"}
    with pytest.raises(ParslPreflightError, match="extra"):
        validate_preflight_result(extra, expectation)

    boolean = {**payload, "sentinel_read": 1}
    with pytest.raises(ParslPreflightError, match="boolean"):
        validate_preflight_result(boolean, expectation)

    duplicate = deepcopy(payload)
    duplicate_paths = duplicate["path_results"]
    assert isinstance(duplicate_paths, list)
    duplicate_paths.append(deepcopy(duplicate_paths[0]))
    with pytest.raises(ParslPreflightError, match="duplicate paths"):
        validate_preflight_result(duplicate, expectation)


def test_expectation_requires_confined_sentinel_and_selected_label(
    tmp_path: Path,
) -> None:
    expectation = _expectation(tmp_path)
    source_plan = resolve_executor_routes(
        [
            _requirement(
                "node",
                expectation.origins[0],
                dependency_path=str(tmp_path.resolve()),
            )
        ],
        executor_bindings={"cpu": _binding()},
    )

    with pytest.raises(ParslPreflightError, match="confined"):
        build_preflight_expectation(
            source_plan,
            "cpu",
            storage_root=tmp_path / "storage",
            sentinel_path=tmp_path / "elsewhere" / "sentinel",
        )

    with pytest.raises(ParslPreflightError, match="non-cache namespace"):
        build_preflight_expectation(
            source_plan,
            "cpu",
            storage_root=tmp_path / "storage",
            sentinel_path=tmp_path / "storage" / "cache" / "v1" / "results",
        )

    with pytest.raises(ParslPreflightError, match="does not select"):
        build_preflight_expectation(
            source_plan,
            "gpu",
            storage_root=tmp_path / "storage",
            sentinel_path=tmp_path / "storage" / ".preflight",
        )


def test_batch_validation_requires_exact_selected_labels(
    tmp_path: Path,
) -> None:
    expectation = _expectation(tmp_path)
    payload = _valid_payload(expectation)

    validated = validate_preflight_results(
        {"cpu": payload},
        {"cpu": expectation},
    )
    assert set(validated) == {"cpu"}

    with pytest.raises(ParslPreflightError, match="missing=.*cpu"):
        validate_preflight_results({}, {"cpu": expectation})

    with pytest.raises(ParslPreflightError, match="extra=.*other"):
        validate_preflight_results(
            {"cpu": payload, "other": payload},
            {"cpu": expectation},
        )
