from __future__ import annotations

import hashlib
import json
import os
from datetime import timedelta
from pathlib import Path

import pytest

from bioimageflow.cluster._common import canonical_digest, thaw_json
from bioimageflow.cluster import _deployment_uv as deployment_uv_module
from bioimageflow.cluster._deployment_snapshot import _stable_artifact
from bioimageflow.cluster import deployment as deployment_module
from bioimageflow.cluster.deployment import prepare_deployment
from bioimageflow.cluster.values import (
    ClusterEnvironment,
    ParslConfiguration,
    RemoteClusterConfig,
    SchedulerJob,
    SetupScript,
)
from bioimageflow.integration import get_execution_capabilities


_REAL_BUILD_LOCAL_DISTRIBUTION = deployment_module._build_local_distribution


def _uv_lock(
    required: tuple[str, ...] = ("bioimageflow-core", "parsl", "psij-python"),
) -> str:
    versions = {
        "bioimageflow-core": "0.3.0",
        "parsl": "2026.5.25",
        "psij-python": "0.9.11",
    }
    dependencies = "\n".join(f'    {{ name = "{name}" }},' for name in required)
    packages = "\n".join(
        f'''[[package]]
name = "{name}"
version = "{versions[name]}"
source = {{ directory = "runtime/{name}" }}
'''
        for name in required
    )
    return f'''version = 1
revision = 3
requires-python = ">=3.10"

[[package]]
name = "example"
version = "1.0"
source = {{ editable = "." }}
dependencies = [
{dependencies}
]

[package.metadata]

[package.metadata.requires-dev]
analysis = []

{packages}'''


_UV_LOCK = _uv_lock()

_BUILD_BACKEND = r"""from __future__ import annotations
import csv
import gzip
import hashlib
import io
import tarfile
import zipfile
from pathlib import Path

NAME = "example"
VERSION = "1.0"

def build_sdist(sdist_directory, config_settings=None):
    filename = f"{NAME}-{VERSION}.tar.gz"
    root = f"{NAME}-{VERSION}"
    with open(Path(sdist_directory) / filename, "wb") as raw:
        with gzip.GzipFile(fileobj=raw, mode="wb", mtime=315532800) as compressed:
            with tarfile.open(fileobj=compressed, mode="w") as archive:
                for relative in ("pyproject.toml", "backend.py", "example/__init__.py"):
                    content = Path(relative).read_bytes()
                    info = tarfile.TarInfo(f"{root}/{relative}")
                    info.size = len(content)
                    info.mtime = 315532800
                    info.mode = 0o644
                    archive.addfile(info, io.BytesIO(content))
    return filename

def build_wheel(wheel_directory, config_settings=None, metadata_directory=None):
    filename = f"{NAME}-{VERSION}-py3-none-any.whl"
    dist_info = f"{NAME}-{VERSION}.dist-info"
    files = {
        "example/__init__.py": Path("example/__init__.py").read_bytes(),
        f"{dist_info}/METADATA": f"Metadata-Version: 2.1\nName: {NAME}\nVersion: {VERSION}\n".encode(),
        f"{dist_info}/WHEEL": b"Wheel-Version: 1.0\nGenerator: fixture\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
    }
    records = []
    for path, content in files.items():
        digest = hashlib.sha256(content).digest()
        import base64
        encoded = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
        records.append((path, f"sha256={encoded}", str(len(content))))
    record_path = f"{dist_info}/RECORD"
    stream = io.StringIO()
    writer = csv.writer(stream, lineterminator="\n")
    writer.writerows([*records, (record_path, "", "")])
    files[record_path] = stream.getvalue().encode()
    with zipfile.ZipFile(Path(wheel_directory) / filename, "w") as archive:
        for path, content in sorted(files.items()):
            info = zipfile.ZipInfo(path, (1980, 1, 1, 0, 0, 0))
            info.external_attr = 0o644 << 16
            archive.writestr(info, content)
    return filename
"""


@pytest.fixture(autouse=True)
def _fast_distribution_builds(monkeypatch):
    def build(
        executable,
        source,
        destination,
        *,
        expected_name,
        expected_version,
    ):
        destination.mkdir(parents=True)
        source_bytes = (
            b"authoritative-bioimageflow-fixture"
            if expected_name == "bioimageflow"
            else b"".join(
                path.relative_to(source).as_posix().encode() + b"\0" + path.read_bytes()
                for path in (
                    source / "pyproject.toml",
                    source / "backend.py",
                    source / "example" / "__init__.py",
                )
            )
            if expected_name == "example"
            else b"".join(
                path.relative_to(source).as_posix().encode() + b"\0" + path.read_bytes()
                for path in sorted(source.rglob("*"))
                if path.is_file()
            )
        )
        digest = hashlib.sha256(source_bytes).digest()
        sdist = destination / f"{expected_name}-{expected_version}.tar.gz"
        wheel = destination / f"{expected_name}-{expected_version}-py3-none-any.whl"
        sdist.write_bytes(b"sdist\0" + digest)
        wheel.write_bytes(b"wheel\0" + digest)
        return {
            "name": expected_name,
            "version": expected_version,
            "build_backend": "fixture",
            "build_requires": [],
            "backend_path": [],
            "source_distribution": _stable_artifact(sdist),
            "wheel": {
                **_stable_artifact(wheel),
                "tags": ["py3-none-any"],
            },
        }

    monkeypatch.setattr(deployment_module, "_build_local_distribution", build)
    monkeypatch.setattr(deployment_uv_module, "_build_local_distribution", build)
    monkeypatch.setattr(
        deployment_module,
        "_validate_uv_frozen",
        lambda executable, project: (
            None
            if "[[package]]" in (project / "uv.lock").read_text(encoding="utf-8")
            else (_ for _ in ()).throw(ValueError("environment-lock-invalid"))
        ),
    )

    def installers(version, content_root):
        path = content_root / "environment" / "installers" / "0" / f"uv-{version}-py3-none-any.whl"
        path.parent.mkdir(parents=True)
        path.write_bytes(b"fixture uv wheel")
        return [
            {
                **_stable_artifact(path),
                "path": path.relative_to(content_root).as_posix(),
                "endpoint": "https://files.pythonhosted.org",
                "tags": ["py3-none-any"],
            }
        ]

    monkeypatch.setattr(
        deployment_uv_module, "_capture_uv_installer_artifacts", installers
    )


def _project(
    root: Path,
    *,
    lock: str = _UV_LOCK,
    factory: str = "def build(runtime):\n    return None\n",
) -> RemoteClusterConfig:
    root.mkdir()
    (root / "pyproject.toml").write_text(
        "[project]\nname = 'example'\nversion = '1.0'\nrequires-python = '>=3.10'\n"
        "\n[dependency-groups]\nanalysis = []\n"
        "\n[build-system]\nrequires = []\nbuild-backend = 'backend'\n"
        "backend-path = ['.']\n",
        encoding="utf-8",
    )
    (root / "backend.py").write_text(_BUILD_BACKEND, encoding="utf-8")
    package = root / "example"
    package.mkdir()
    (package / "__init__.py").write_text("VALUE = 1\n", encoding="utf-8")
    for runtime_name in ("bioimageflow-core", "parsl", "psij-python"):
        runtime_version = {
            "bioimageflow-core": "0.3.0",
            "parsl": "2026.5.25",
            "psij-python": "0.9.11",
        }[runtime_name]
        runtime = root / "runtime" / runtime_name
        runtime.mkdir(parents=True)
        (runtime / "pyproject.toml").write_text(
            f"[project]\nname = '{runtime_name}'\nversion = '{runtime_version}'\n",
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

    with (
        prepare_deployment(first_config) as first,
        prepare_deployment(second_config) as second,
    ):
        assert first.deployment_id == second.deployment_id
        assert first.manifest["manifest_digest"] == second.manifest["manifest_digest"]
        serialized = json.dumps(first.manifest)
        assert str(tmp_path) not in serialized
        assert "PRIVATE_INDEX" in serialized
        assert "PARSL_CREDENTIAL" in serialized


def test_prepared_deployment_manifest_is_immutable_and_self_consistent(
    tmp_path: Path,
) -> None:
    config = _project(tmp_path / "project")

    with prepare_deployment(config) as prepared:
        identity = {
            key: thaw_json(value)
            for key, value in prepared.manifest.items()
            if key not in {"deployment_id", "manifest_digest"}
        }
        assert canonical_digest(identity) == prepared.deployment_id
        assert prepared.manifest["manifest_digest"] == prepared.deployment_id
        with pytest.raises(TypeError):
            prepared.manifest["scheduler"] = "pbs"  # type: ignore[index]
        mutable_view = prepared.manifest
        with pytest.raises(TypeError):
            mutable_view |= {"scheduler": "pbs"}
        environment = prepared.manifest["environment"]
        with pytest.raises(TypeError):
            environment["kind"] = "pixi"  # type: ignore[index]
        prepared.verify()


@pytest.mark.parametrize(
    ("changed", "value"),
    [
        ("lock", _UV_LOCK + "\n# Alternate canonical lock serialization.\n"),
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

    assert captured_lock.read_text(encoding="utf-8") == _UV_LOCK
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

    with (
        prepare_deployment(config("/opt/python/a")) as first,
        prepare_deployment(config("/opt/python/b")) as second,
    ):
        assert first.deployment_id != second.deployment_id


def test_local_setup_script_bytes_are_identity_bearing(tmp_path) -> None:
    first_config = _project(tmp_path / "first")
    second_config = _project(tmp_path / "second")
    object.__setattr__(
        second_config,
        "setup",
        SetupScript.from_text("module load Python/3.11\n"),
    )

    with (
        prepare_deployment(first_config) as first,
        prepare_deployment(second_config) as second,
    ):
        assert first.deployment_id != second.deployment_id


def test_declared_local_project_sources_are_snapshotted_but_unrelated_files_are_not(
    tmp_path,
) -> None:
    first_config = _project(tmp_path / "first")
    second_config = _project(tmp_path / "second")
    for config, value, note in (
        (first_config, "VALUE = 1\n", "first note\n"),
        (second_config, "VALUE = 1\n", "different unrelated note\n"),
    ):
        project = Path(config.environment.source)  # type: ignore[union-attr]
        package = project / "example"
        (package / "__init__.py").write_text(value, encoding="utf-8")
        (project / "notes.txt").write_text(note, encoding="utf-8")

    with (
        prepare_deployment(first_config) as first,
        prepare_deployment(second_config) as second,
    ):
        assert first.deployment_id == second.deployment_id
        captured = (
            first.root
            / "environment"
            / "project-inputs"
            / "0"
            / "example"
            / "__init__.py"
        )
        assert captured.read_text(encoding="utf-8") == "VALUE = 1\n"
        assert not any(path.name == "notes.txt" for path in first.root.rglob("*"))

    package = Path(second_config.environment.source) / "example"  # type: ignore[union-attr]
    (package / "__init__.py").write_text("VALUE = 2\n", encoding="utf-8")
    with (
        prepare_deployment(first_config) as first,
        prepare_deployment(second_config) as second,
    ):
        assert first.deployment_id != second.deployment_id


def test_project_source_snapshot_rejects_symlinks_and_hard_links(tmp_path) -> None:
    config = _project(tmp_path / "project")
    project = Path(config.environment.source)  # type: ignore[union-attr]
    package = project / "example"
    source = package / "source.py"
    source.write_text("VALUE = 1\n", encoding="utf-8")
    alias = package / "alias.py"
    os.link(source, alias)

    with pytest.raises(ValueError, match="hard links"):
        prepare_deployment(config)


def _wheelhouse_config(tmp_path: Path, *, wheel_bytes: bytes) -> RemoteClusterConfig:
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    wheel = wheelhouse / "example-1.0-py3-none-any.whl"
    wheel.write_bytes(wheel_bytes)
    digest = hashlib.sha256(wheel_bytes).hexdigest()
    lock = tmp_path / "pylock.toml"
    lock.write_text(
        'lock-version = "1.0"\n'
        '[[packages]]\nname = "example"\nversion = "1.0"\n'
        '[[packages.wheels]]\nname = "example-1.0-py3-none-any.whl"\n'
        f"size = {len(wheel_bytes)}\n"
        f'[packages.wheels.hashes]\nsha256 = "{digest}"\n',
        encoding="utf-8",
    )
    factory = tmp_path / "factory.py"
    factory.write_text("def build(runtime):\n    return None\n", encoding="utf-8")
    return RemoteClusterConfig(
        "login.example",
        "/cluster/alice/bioimageflow",
        environment=ClusterEnvironment.from_wheelhouse(wheelhouse, lock=lock),
        parsl=ParslConfiguration.from_file(factory),
        orchestrator=SchedulerJob("slurm", timedelta(minutes=30)),
    )


def test_wheelhouse_snapshot_requires_locked_hash_and_size(tmp_path) -> None:
    config = _wheelhouse_config(tmp_path, wheel_bytes=b"not-a-real-wheel-but-exact")

    with prepare_deployment(config) as prepared:
        captured = prepared.root / "environment" / "1" / "example-1.0-py3-none-any.whl"
        assert captured.read_bytes() == b"not-a-real-wheel-but-exact"

    wheel = Path(config.environment.source) / "example-1.0-py3-none-any.whl"  # type: ignore[union-attr]
    wheel.write_bytes(b"tampered")
    with pytest.raises(ValueError, match="SHA-256"):
        prepare_deployment(config)


def test_wheelhouse_snapshot_rejects_unlocked_extra_wheels(tmp_path) -> None:
    config = _wheelhouse_config(tmp_path, wheel_bytes=b"locked")
    wheelhouse = Path(config.environment.source)  # type: ignore[union-attr]
    (wheelhouse / "other-1.0-py3-none-any.whl").write_bytes(b"other")

    with pytest.raises(ValueError, match="unlocked wheels"):
        prepare_deployment(config)


def test_environment_capabilities_report_only_end_to_end_support() -> None:
    capabilities = get_execution_capabilities().capabilities

    assert capabilities["managed_uv_environment"].supported
    for name in (
        "managed_pixi_environment",
        "managed_pylock_environment",
        "offline_wheelhouse_environment",
    ):
        assert not capabilities[name].supported
        assert capabilities[name].reason
    assert capabilities["existing_python_attestation"].supported
    for name in (
        "managed_setup_scripts",
        "remote_cluster_validation",
        "remote_cluster_planning",
        "idempotent_planned_submission",
        "durable_remote_diagnostics",
        "cluster_cleanup_planning",
    ):
        assert capabilities[name].supported
