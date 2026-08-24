from __future__ import annotations

import json
from pathlib import Path

import pytest

from bioimageflow.cluster import deployment as deployment_module
from bioimageflow.cluster.deployment import prepare_deployment
from tests.unit.cluster.test_deployment import (
    _REAL_BUILD_LOCAL_DISTRIBUTION,
    _UV_LOCK,
    _fast_distribution_builds,  # noqa: F401 - imported autouse fixture
    _project,
    _uv_lock,
)

def test_uv_plan_contains_frozen_authoritative_artifacts_without_local_paths(
    tmp_path: Path,
) -> None:
    config = _project(tmp_path / "project")

    with prepare_deployment(config) as prepared:
        plan = prepared.manifest["environment_plan"]
        assert plan["schema"] == "bioimageflow.uv_install_plan.v1"
        assert plan["frozen"] is True
        assert plan["network_resolution"] is False
        assert plan["target_policy"] == "captured-wheels-target-selected"
        assert plan["psij_scheduler_plugin"] == {
            "scheduler": "slurm",
            "distribution": "psij-python",
            "version": "0.9.11",
        }
        assert {item["name"] for item in plan["local_artifacts"]} == {
            "bioimageflow",
            "bioimageflow-core",
            "example",
            "parsl",
            "psij-python",
        }
        assert all(
            artifact["wheel"]["digest"].startswith("sha256:")
            for artifact in plan["local_artifacts"]
        )
        serialized = json.dumps(prepared.manifest)
        assert str(tmp_path) not in serialized
        assert (prepared.root / "environment" / "install-plan.json").is_file()


def test_uv_preparation_rejects_a_stale_or_malformed_lock(tmp_path: Path) -> None:
    config = _project(tmp_path / "project", lock="version = 1\n")

    with pytest.raises(ValueError, match="environment-lock-invalid"):
        prepare_deployment(config)


@pytest.mark.parametrize(
    "missing", ("bioimageflow-core", "parsl", "psij-python")
)
def test_uv_preparation_rejects_a_missing_required_runtime_distribution(
    tmp_path: Path, missing: str
) -> None:
    required = tuple(
        name
        for name in ("bioimageflow-core", "parsl", "psij-python")
        if name != missing
    )
    config = _project(tmp_path / "project", lock=_uv_lock(required))

    with pytest.raises(ValueError, match="environment-build-lock-incomplete"):
        prepare_deployment(config)


def test_uv_preparation_rejects_an_incompatible_locked_bioimageflow(
    tmp_path: Path,
) -> None:
    lock = _UV_LOCK + '''
[[package]]
name = "bioimageflow"
version = "999.0"
source = { directory = "." }
'''
    config = _project(tmp_path / "project", lock=lock)

    with pytest.raises(ValueError, match="bioimageflow-version-conflict"):
        prepare_deployment(config)


def test_local_distribution_build_is_offline_non_editable_and_hashed(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    _project(project)
    executable, _version = deployment_module._uv_executable_and_version()

    result = _REAL_BUILD_LOCAL_DISTRIBUTION(
        executable,
        project,
        tmp_path / "artifacts",
        expected_name="example",
        expected_version="1.0",
    )

    assert result["source_distribution"]["digest"].startswith("sha256:")
    assert result["wheel"]["digest"].startswith("sha256:")
    assert result["wheel"]["tags"] == ["py3-none-any"]
    assert "editable" not in json.dumps(result).lower()
