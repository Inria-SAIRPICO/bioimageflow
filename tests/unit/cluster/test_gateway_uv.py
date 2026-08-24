from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest
from packaging.tags import sys_tags

from bioimageflow.cluster._gateway_support import GatewayOperationFailure
from bioimageflow.cluster import _gateway_uv as gateway_uv_module
from bioimageflow.cluster._gateway_uv import (
    _select_uv_installer,
    _select_wheels,
    _validate_required_runtime,
    realize_managed_uv,
)
from bioimageflow.cluster.gateway_artifact import build_gateway_artifact


def _artifact(root: Path, relative: str, content: bytes) -> dict[str, object]:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return {
        "filename": path.name,
        "path": relative,
        "size": len(content),
        "digest": f"sha256:{hashlib.sha256(content).hexdigest()}",
    }


def test_gateway_selects_only_hash_verified_target_wheels(tmp_path: Path) -> None:
    local = _artifact(
        tmp_path,
        "environment/artifacts/bioimageflow/bioimageflow-0.6.0-py3-none-any.whl",
        b"authoritative",
    )
    dependency = _artifact(
        tmp_path,
        "environment/registry/0/example-1.0-py3-none-any.whl",
        b"locked",
    )
    plan = {
        "requires_python": f">={sys.version_info.major}.{sys.version_info.minor}",
        "local_artifacts": [
            {
                "name": "bioimageflow",
                "version": "0.6.0",
                "wheel": local,
            }
        ],
        "locked_packages": [
            {
                "name": "example",
                "version": "1.0",
                "source_kind": "registry",
                "artifacts": [dependency],
            }
        ],
    }

    wheels, expected = _select_wheels(
        tmp_path, plan, {"bioimageflow_version": "0.6.0"}
    )

    assert [path.name for path in wheels] == [
        "bioimageflow-0.6.0-py3-none-any.whl",
        "example-1.0-py3-none-any.whl",
    ]
    assert expected == {"bioimageflow": "0.6.0", "example": "1.0"}

    (tmp_path / dependency["path"]).write_bytes(b"changed")  # type: ignore[operator]
    with pytest.raises(GatewayOperationFailure) as failure:
        _select_wheels(tmp_path, plan, {"bioimageflow_version": "0.6.0"})
    assert failure.value.diagnostic["category"] == "environment-artifact-missing"


def test_gateway_rejects_a_deficient_runtime_plan_with_stable_category() -> None:
    with pytest.raises(GatewayOperationFailure) as failure:
        _validate_required_runtime(
            {
                "psij_scheduler_plugin": {
                    "scheduler": "slurm",
                    "distribution": "psij-python",
                    "version": "1.0",
                }
            },
            {"scheduler": "slurm"},
            {"bioimageflow": "0.6.0", "psij-python": "1.0"},
        )

    assert failure.value.diagnostic["category"] == "environment-build-lock-incomplete"


def test_gateway_extracts_compatible_pinned_uv_executable(tmp_path: Path) -> None:
    wheel_bytes_path = tmp_path / "source.whl"
    with zipfile.ZipFile(wheel_bytes_path, "w") as archive:
        member = zipfile.ZipInfo("uv/uv")
        member.external_attr = 0o755 << 16
        archive.writestr(member, b"#!/bin/sh\nexit 0\n")
    wheel_bytes = wheel_bytes_path.read_bytes()
    wheel = _artifact(
        tmp_path / "content",
        "environment/installers/0/uv-0.9.0-py3-none-any.whl",
        wheel_bytes,
    )
    installer = {"version": "0.9.0", "artifacts": [wheel]}
    (tmp_path / "candidate").mkdir()

    executable = _select_uv_installer(
        tmp_path / "candidate", tmp_path / "content", installer
    )

    assert executable.read_bytes() == b"#!/bin/sh\nexit 0\n"
    assert executable.stat().st_mode & 0o111


def test_isolated_gateway_artifact_selects_uv_with_vendored_packaging(
    tmp_path: Path,
) -> None:
    wheel_bytes_path = tmp_path / "source.whl"
    with zipfile.ZipFile(wheel_bytes_path, "w") as archive:
        member = zipfile.ZipInfo("uv/uv")
        member.external_attr = 0o755 << 16
        archive.writestr(member, b"#!/bin/sh\nexit 0\n")
    content = tmp_path / "content"
    wheel = _artifact(
        content,
        "environment/installers/0/uv-0.9.0-py3-none-any.whl",
        wheel_bytes_path.read_bytes(),
    )
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    script = (
        "import json,sys; from pathlib import Path; "
        "sys.path.insert(0,sys.argv[1]); "
        "from bioimageflow.cluster._gateway_uv import _select_uv_installer; "
        "installer=json.loads(sys.argv[4]); "
        "print(_select_uv_installer(Path(sys.argv[2]),Path(sys.argv[3]),installer).name)"
    )
    with build_gateway_artifact() as artifact:
        selected = subprocess.run(
            [
                sys.executable,
                "-I",
                "-c",
                script,
                str(artifact.path),
                str(candidate),
                str(content),
                json.dumps({"version": "0.9.0", "artifacts": [wheel]}),
            ],
            cwd=tmp_path,
            capture_output=True,
            check=False,
            text=True,
        )

    assert selected.returncode == 0, selected.stderr
    assert selected.stdout.strip() == "uv"


def _distribution_wheel(path: Path, name: str, version: str) -> None:
    normalized = name.replace("-", "_")
    dist_info = f"{normalized}-{version}.dist-info"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            f"{dist_info}/METADATA",
            f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n",
        )
        archive.writestr(
            f"{dist_info}/WHEEL",
            "Wheel-Version: 1.0\nGenerator: fixture\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        )
        archive.writestr(f"{dist_info}/RECORD", "")


def test_realize_managed_uv_uses_only_captured_wheels(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    local_uv = shutil.which("uv")
    if local_uv is None:
        pytest.skip("uv is required for the managed installer integration fixture")
    uv_version = subprocess.run(
        [local_uv, "--version"], capture_output=True, check=True, text=True
    ).stdout.split()[1]
    target_tag = next(sys_tags())
    content = tmp_path / "content"
    uv_wheel_path = content / "environment" / "installers" / "0" / (
        f"uv-{uv_version}-{target_tag}.whl"
    )
    uv_wheel_path.parent.mkdir(parents=True)
    with zipfile.ZipFile(uv_wheel_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        member = zipfile.ZipInfo("uv/uv")
        member.external_attr = 0o755 << 16
        archive.writestr(member, Path(local_uv).resolve().read_bytes())
    bioimageflow_path = (
        content
        / "environment"
        / "artifacts"
        / "bioimageflow"
        / "bioimageflow-0.6.0-py3-none-any.whl"
    )
    bioimageflow_path.parent.mkdir(parents=True)
    _distribution_wheel(bioimageflow_path, "bioimageflow", "0.6.0")
    runtime_paths: dict[str, Path] = {}
    for name in ("bioimageflow-core", "parsl", "psij-python"):
        path = (
            content
            / "environment"
            / "artifacts"
            / name
            / f"{name.replace('-', '_')}-1.0-py3-none-any.whl"
        )
        path.parent.mkdir(parents=True)
        _distribution_wheel(path, name, "1.0")
        runtime_paths[name] = path

    uv_artifact = _artifact(
        content,
        uv_wheel_path.relative_to(content).as_posix(),
        uv_wheel_path.read_bytes(),
    )
    local_artifact = _artifact(
        content,
        bioimageflow_path.relative_to(content).as_posix(),
        bioimageflow_path.read_bytes(),
    )
    plan = {
        "schema": "bioimageflow.uv_install_plan.v1",
        "frozen": True,
        "network_resolution": False,
        "target_policy": "captured-wheels-target-selected",
        "requires_python": f">={sys.version_info.major}.{sys.version_info.minor}",
        "installer": {
            "name": "uv",
            "version": uv_version,
            "artifacts": [uv_artifact],
        },
        "local_artifacts": [
            {
                "name": "bioimageflow",
                "version": "0.6.0",
                "wheel": local_artifact,
            },
            *[
                {
                    "name": name,
                    "version": "1.0",
                    "wheel": _artifact(
                        content,
                        path.relative_to(content).as_posix(),
                        path.read_bytes(),
                    ),
                }
                for name, path in runtime_paths.items()
            ],
        ],
        "locked_packages": [],
        "psij_scheduler_plugin": {
            "scheduler": "slurm",
            "distribution": "psij-python",
            "version": "1.0",
        },
    }
    expected_attestation = {
        "schema": "bioimageflow.cluster.managed_uv_attestation.v1",
        "packages": {"bioimageflow": "0.6.0"},
    }

    def attest(environment, expected, scheduler, *, failure_category):
        assert expected == {
            "bioimageflow": "0.6.0",
            "bioimageflow-core": "1.0",
            "parsl": "1.0",
            "psij-python": "1.0",
        }
        assert scheduler == "slurm"
        assert failure_category == "deployment-install-failed"
        assert (environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")).exists()
        return expected_attestation, "sha256:" + "a" * 64

    monkeypatch.setattr(gateway_uv_module, "_attest_environment", attest)
    candidate = tmp_path / "candidate"
    candidate.mkdir()

    attestation, digest, inventory = realize_managed_uv(
        candidate,
        content,
        {
            "environment_plan": plan,
            "bioimageflow_version": "0.6.0",
            "scheduler": "slurm",
        },
    )

    assert attestation == expected_attestation
    assert digest == "sha256:" + "a" * 64
    assert inventory.startswith("sha256:")
    assert not (candidate / ".uv-cache").exists()
