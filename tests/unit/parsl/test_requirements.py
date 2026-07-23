"""Canonical Parsl worker-requirement construction."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from bioimageflow.cache import compute_env_hash
from bioimageflow.parsl.requirements import (
    WorkerRequirementError,
    anchored_dependency_paths,
    build_worker_requirement,
    canonical_environment_identity,
    normalize_core_requirement,
    normalize_resource_request,
    parse_memory_bytes,
)
from bioimageflow_core import (
    EnvironmentSpec,
    IOModel,
    ProcessingTool,
    ResourceSpec,
)
from bioimageflow_core.worker_origins import InstalledModuleOriginV1


class RequirementTool(ProcessingTool):
    environment = EnvironmentSpec(
        name="analysis",
        dependencies={
            "python": "3.12",
            "pip": [
                "numpy==2.4.2",
                "bioimageflow-core @ file:///shared/core",
            ],
            "local": [
                {
                    "name": "tools",
                    "path": "/shared/tools/../tools",
                    "editable": True,
                }
            ],
        },
    )
    resources = ResourceSpec(
        cpu=3,
        gpu=1,
        memory="8GB",
        gpu_memory="4GiB",
        max_concurrent=2,
    )

    class Outputs(IOModel):
        value: int

    def process_row(self, arguments):
        raise AssertionError("requirement construction must not execute tools")


def _origin() -> InstalledModuleOriginV1:
    return InstalledModuleOriginV1(
        distribution="example-tools",
        version="1.0.0",
        module="example_tools.worker",
        class_name="RequirementTool",
    )


def test_builds_complete_canonical_requirement() -> None:
    requirement = build_worker_requirement(
        "outer/analyze",
        RequirementTool(),
        _origin(),
        core_requirement=" bioimageflow_core >=0.1.7, <0.2 ",
    )

    assert requirement.scoped_node_name == "outer/analyze"
    assert requirement.environment_name == "analysis"
    assert requirement.dependency_hash == compute_env_hash(
        RequirementTool.environment.dependencies
    )
    assert requirement.core_requirement == "bioimageflow-core>=0.1.7,<0.2"
    assert requirement.anchored_dependency_paths == (
        str(Path("/shared/core").resolve()),
        str(Path("/shared/tools").resolve()),
    )
    assert requirement.resources.cpu == 3
    assert requirement.resources.gpu == 1
    assert requirement.resources.memory_bytes == 8 * 1024**3
    assert requirement.resources.gpu_memory_bytes == 4 * 1024**3
    assert requirement.resources.max_concurrent == 2
    assert requirement.tool_origin == _origin()
    assert requirement.tool_origin_mode == "installed_module"
    assert requirement.environment_identity.startswith("env_")
    assert len(requirement.environment_identity) == 68


def test_environment_identity_includes_every_identity_field() -> None:
    baseline = canonical_environment_identity(
        name="analysis",
        dependency_hash="a" * 64,
        allow_flexible_versions=False,
    )

    assert baseline == canonical_environment_identity(
        name="analysis",
        dependency_hash="a" * 64,
        allow_flexible_versions=False,
    )
    assert baseline != canonical_environment_identity(
        name="other",
        dependency_hash="a" * 64,
        allow_flexible_versions=False,
    )
    assert baseline != canonical_environment_identity(
        name="analysis",
        dependency_hash="b" * 64,
        allow_flexible_versions=False,
    )
    assert baseline != canonical_environment_identity(
        name="analysis",
        dependency_hash="a" * 64,
        allow_flexible_versions=True,
    )


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("1B", 1),
        ("2KB", 2 * 1024),
        ("3MB", 3 * 1024**2),
        ("4GB", 4 * 1024**3),
        ("5TiB", 5 * 1024**4),
    ],
)
def test_memory_parser_uses_one_integral_unit_contract(
    value: str,
    expected: int,
) -> None:
    assert parse_memory_bytes(value) == expected


@pytest.mark.parametrize(
    "value",
    ["", "1", "0GB", "1.5GB", " 1GB", "1gb", "1PB", True],
)
def test_memory_parser_rejects_noncanonical_values(value: object) -> None:
    with pytest.raises((TypeError, ValueError), match="memory"):
        parse_memory_bytes(value)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("cpu", True),
        ("cpu", 0),
        ("gpu", False),
        ("gpu", -1),
        ("max_concurrent", True),
        ("max_concurrent", -1),
    ],
)
def test_resource_normalization_rejects_invalid_integers(
    field: str,
    value: object,
) -> None:
    kwargs = {
        "cpu": 1,
        "gpu": 0,
        "memory": None,
        "gpu_memory": None,
        "max_concurrent": 0,
        field: value,
    }
    with pytest.raises((TypeError, ValueError), match=field):
        normalize_resource_request(ResourceSpec(**kwargs))


def test_zero_max_concurrent_remains_unlimited_default() -> None:
    resources = normalize_resource_request(ResourceSpec(max_concurrent=0))

    assert resources.max_concurrent == 0


def test_anchored_dependency_paths_reject_relative_and_remote_file_uris() -> None:
    with pytest.raises(WorkerRequirementError, match="absolute"):
        anchored_dependency_paths({"local": [{"path": "../tools"}]})

    with pytest.raises(WorkerRequirementError, match="Unsupported"):
        anchored_dependency_paths({"pip": ["tools @ file://remote/shared/tools"]})


@pytest.mark.parametrize(
    "value",
    [
        "",
        "bioimageflow-core",
        "other-core>=1",
        "api-v1",
    ],
)
def test_core_requirement_must_name_and_constrain_core(value: str) -> None:
    with pytest.raises(WorkerRequirementError, match="core_requirement"):
        normalize_core_requirement(value)


@dataclass
class WorkflowEnvironmentSettings:
    name: str = "analysis"
    max_workers: int = 0
    worker_env: object | None = None
    worker_timeout: float | None = None


@pytest.mark.parametrize(
    "settings",
    [
        WorkflowEnvironmentSettings(max_workers=2),
        WorkflowEnvironmentSettings(worker_env=object()),
        WorkflowEnvironmentSettings(worker_timeout=10),
    ],
)
def test_parsl_rejects_wetlands_only_environment_settings(
    settings: WorkflowEnvironmentSettings,
) -> None:
    with pytest.raises(WorkerRequirementError, match="Wetlands-only"):
        build_worker_requirement(
            "analyze",
            RequirementTool(),
            _origin(),
            core_requirement="bioimageflow-core>=0.1.7,<0.2",
            workflow_environment=settings,
        )


def test_requirement_construction_does_not_require_local_paths_to_exist() -> None:
    paths = anchored_dependency_paths(
        {"local": [{"path": "/shared/not-mounted-on-test-host"}]}
    )

    assert paths == ("/shared/not-mounted-on-test-host",)
