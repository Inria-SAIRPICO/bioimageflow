from __future__ import annotations

import hashlib
import os
from datetime import timedelta
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from typing import Any

import pytest

from bioimageflow import PSIJLaunchConfig, PreLaunchScript
from bioimageflow.launcher.errors import LauncherProtocolError
from bioimageflow.launcher.psij import _build_spec
from bioimageflow.launcher.psij_artifacts import install_intent, read_intent
from bioimageflow.launcher.pre_launch import (
    MAX_PRE_LAUNCH_BYTES,
    PRE_LAUNCH_RELATIVE_PATH,
    install_prepared_pre_launch,
    pre_launch_from_bundle_request,
    prepare_pre_launch,
    stage_bundle_pre_launch,
)
from bioimageflow.launcher.repository import LauncherRepository
from tests.unit.launcher.helpers import launcher_submission


def _digest(content: bytes) -> str:
    return f"sha256:{hashlib.sha256(content).hexdigest()}"


def test_text_script_is_validated_and_redacted() -> None:
    script = PreLaunchScript.from_text("export VALUE='private value'\n")

    assert script.source_kind == "text"
    assert "private value" not in repr(script)

    for invalid in ("", "contains\x00nul", "\ud800"):
        with pytest.raises(ValueError):
            PreLaunchScript.from_text(invalid)
    with pytest.raises(ValueError, match="64 KiB"):
        PreLaunchScript.from_text("x" * (MAX_PRE_LAUNCH_BYTES + 1))
    with pytest.raises(TypeError):
        PreLaunchScript.from_text(b"echo no")  # type: ignore[arg-type]


def test_local_file_is_snapshotted_once_and_installed_read_only(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source script.sh"
    content = b"export MODULEPATH=/shared/modules\n"
    source.write_bytes(content)
    script = PreLaunchScript.from_local_file(source)

    with prepare_pre_launch(script) as prepared:
        assert prepared is not None
        source.write_bytes(b"changed after snapshot\n")
        candidate = tmp_path / "candidate"
        candidate.mkdir()
        metadata = install_prepared_pre_launch(prepared, candidate)

    artifact = candidate / PRE_LAUNCH_RELATIVE_PATH
    assert artifact.read_bytes() == content
    assert artifact.stat().st_mode & 0o777 == 0o400
    assert metadata == {
        "source_kind": "uploaded",
        "source_path": None,
        "expected_digest": None,
        "artifact": {
            "path": PRE_LAUNCH_RELATIVE_PATH,
            "size": len(content),
            "digest": _digest(content),
        },
    }


def test_local_file_rejects_symlink_and_non_utf8(tmp_path: Path) -> None:
    regular = tmp_path / "regular.sh"
    regular.write_text("echo ok\n")
    link = tmp_path / "link.sh"
    os.symlink(regular, link)

    with pytest.raises(ValueError, match="non-symlink"):
        with prepare_pre_launch(PreLaunchScript.from_local_file(link)):
            pass

    invalid = tmp_path / "invalid.sh"
    invalid.write_bytes(b"\xff")
    with pytest.raises(ValueError, match="UTF-8"):
        with prepare_pre_launch(PreLaunchScript.from_local_file(invalid)):
            pass


def test_cluster_file_optional_digest_is_checked_before_install(
    tmp_path: Path,
) -> None:
    source = tmp_path / "cluster-init.sh"
    content = b"module load python\n"
    source.write_bytes(content)
    path = PurePosixPath(source.as_posix())

    script = PreLaunchScript.from_cluster_file(
        path,
        expected_digest=_digest(content),
    )
    assert script.source_kind == "cluster_file"
    assert source.as_posix() not in repr(script)
    with prepare_pre_launch(script) as prepared:
        assert prepared is not None
        assert prepared.source_kind == "cluster_file"
        assert prepared.source_path == source.as_posix()
        assert prepared.digest == _digest(content)

    mismatched = PreLaunchScript.from_cluster_file(
        path,
        expected_digest="sha256:" + "0" * 64,
    )
    with pytest.raises(ValueError, match="expected digest"):
        with prepare_pre_launch(mismatched):
            pass


@pytest.mark.parametrize(
    "path",
    ["relative.sh", "/cluster/../escape.sh", "//cluster/script.sh", "/a/"],
)
def test_cluster_file_requires_normalized_absolute_path(path: str) -> None:
    with pytest.raises(ValueError):
        PreLaunchScript.from_cluster_file(path)


def test_bundle_stages_uploaded_bytes_and_describes_cluster_source(
    tmp_path: Path,
) -> None:
    content = b"export BIF_ENV=ready\n"
    uploaded, uploaded_external = stage_bundle_pre_launch(
        PreLaunchScript.from_text(content.decode()),
        tmp_path,
    )
    assert uploaded_external == ()
    assert uploaded == {
        "source_kind": "uploaded",
        "source_path": PRE_LAUNCH_RELATIVE_PATH,
        "expected_digest": _digest(content),
        "expected_size": len(content),
    }
    staged = pre_launch_from_bundle_request(uploaded, tmp_path)
    with prepare_pre_launch(staged) as prepared:
        assert prepared is not None
        assert prepared.path.read_bytes() == content

    cluster, external = stage_bundle_pre_launch(
        PreLaunchScript.from_cluster_file("/shared/site/init.sh"),
        tmp_path,
    )
    assert cluster == {
        "source_kind": "cluster_file",
        "source_path": "/shared/site/init.sh",
        "expected_digest": None,
        "expected_size": None,
    }
    assert external == (
        {
            "kind": "cluster_pre_launch",
            "path": "/shared/site/init.sh",
            "expected_digest": None,
        },
    )


def test_psij_intent_binds_exact_run_owned_path_and_detects_tampering(
    tmp_path: Path,
) -> None:
    class Record:
        def __init__(self, **values: Any) -> None:
            self.__dict__.update(values)

    storage = tmp_path / "storage with spaces"
    repository = LauncherRepository(storage)
    run_id = repository.new_run_id()
    candidate = repository.create_candidate(run_id)
    launch = PSIJLaunchConfig(
        executor="slurm",
        walltime=timedelta(minutes=10),
    )
    submission = launcher_submission(storage, run_id)
    submission["launch"] = launch.to_dict()
    with prepare_pre_launch(
        PreLaunchScript.from_text("export SITE_ENV=ready\n")
    ) as prepared:
        submission["psij_pre_launch"] = install_prepared_pre_launch(
            prepared,
            candidate,
        )
    control = repository.allocate(
        submission,
        backend="psij",
        candidate_dir=candidate,
    )
    work_dir = control.confined_path("psij/executor")
    work_dir.mkdir(parents=True)

    intent, created = install_intent(control, launch, work_dir)
    runtime = SimpleNamespace(
        ResourceSpecV1=Record,
        JobAttributes=Record,
        JobSpec=Record,
    )
    spec = _build_spec(runtime, intent["job"])
    artifact = control.control_dir / PRE_LAUNCH_RELATIVE_PATH

    assert created
    assert spec.pre_launch == artifact
    assert intent["job"]["pre_launch"]["path"] == str(artifact)
    assert intent["job"]["pre_launch"]["digest"].startswith("sha256:")

    artifact.chmod(0o600)
    artifact.write_text("changed\n")
    with pytest.raises(LauncherProtocolError, match="does not match"):
        read_intent(control)
