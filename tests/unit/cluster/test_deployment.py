from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import pytest

from bioimageflow.cluster.deployment import prepare_deployment
from bioimageflow.cluster.values import (
    ClusterEnvironment,
    ParslConfiguration,
    RemoteClusterConfig,
    SchedulerJob,
    SetupScript,
)


def _project(root: Path, *, lock: str = "version = 1\n", factory: str = "def build(runtime):\n    return None\n") -> RemoteClusterConfig:
    root.mkdir()
    (root / "pyproject.toml").write_text(
        "[project]\nname = 'example'\nversion = '1.0'\n",
        encoding="utf-8",
    )
    (root / "uv.lock").write_text(lock, encoding="utf-8")
    parsl = root / "parsl.py"
    parsl.write_text(factory, encoding="utf-8")
    return RemoteClusterConfig(
        "login.example",
        "/cluster/alice/bioimageflow",
        environment=ClusterEnvironment.from_uv_project(
            root,
            groups=("analysis",),
            auth_refs={"index": "PRIVATE_INDEX"},
        ),
        parsl=ParslConfiguration.from_file(
            parsl,
            kwargs={"workers": 2},
            secret_refs={"credential": "PARSL_CREDENTIAL"},
        ),
        orchestrator=SchedulerJob(
            "slurm",
            timedelta(hours=2),
            queue="compute",
        ),
        setup=SetupScript.from_text("module load Python/3.12\n"),
    )


def test_equal_deployment_contents_have_path_independent_identity(tmp_path) -> None:
    first_config = _project(tmp_path / "first")
    second_config = _project(tmp_path / "second")

    with prepare_deployment(first_config) as first, prepare_deployment(second_config) as second:
        assert first.deployment_id == second.deployment_id
        assert first.manifest["manifest_digest"] == second.manifest["manifest_digest"]
        serialized = json.dumps(first.manifest)
        assert str(tmp_path) not in serialized
        assert "PRIVATE_INDEX" in serialized
        assert "PARSL_CREDENTIAL" in serialized


@pytest.mark.parametrize(
    ("changed", "value"),
    [
        ("lock", "version = 2\n"),
        ("factory", "def build(runtime):\n    raise RuntimeError\n"),
    ],
)
def test_deployment_identity_is_sensitive_to_source_bytes(
    tmp_path,
    changed: str,
    value: str,
) -> None:
    baseline = _project(tmp_path / "baseline")
    kwargs = {changed: value}
    modified = _project(tmp_path / "modified", **kwargs)

    with prepare_deployment(baseline) as first, prepare_deployment(modified) as second:
        assert first.deployment_id != second.deployment_id


def test_deployment_snapshot_does_not_reread_original_sources(tmp_path) -> None:
    project = tmp_path / "project"
    config = _project(project)
    prepared = prepare_deployment(config)
    captured_lock = prepared.root / "environment" / "1" / "uv.lock"

    (project / "uv.lock").write_text("mutated = true\n", encoding="utf-8")
    (project / "parsl.py").unlink()

    assert captured_lock.read_text(encoding="utf-8") == "version = 1\n"
    prepared.verify()
    captured_lock.write_text("tampered = true\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="no longer match"):
        prepared.verify()
    prepared.close()


def test_existing_python_path_remains_an_external_identity(tmp_path) -> None:
    factory = tmp_path / "factory.py"
    factory.write_text("def build(runtime):\n    return None\n", encoding="utf-8")

    def config(python: str) -> RemoteClusterConfig:
        return RemoteClusterConfig(
            "login.example",
            "/cluster/alice/bioimageflow",
            environment=ClusterEnvironment.from_existing_python(python),
            parsl=ParslConfiguration.from_file(factory),
            orchestrator=SchedulerJob("slurm", timedelta(minutes=30)),
        )

    with prepare_deployment(config("/opt/python/a")) as first, prepare_deployment(
        config("/opt/python/b")
    ) as second:
        assert first.deployment_id != second.deployment_id


def test_local_setup_script_bytes_are_identity_bearing(tmp_path) -> None:
    first_config = _project(tmp_path / "first")
    second_config = _project(tmp_path / "second")
    object.__setattr__(
        second_config,
        "setup",
        SetupScript.from_text("module load Python/3.11\n"),
    )

    with prepare_deployment(first_config) as first, prepare_deployment(second_config) as second:
        assert first.deployment_id != second.deployment_id
