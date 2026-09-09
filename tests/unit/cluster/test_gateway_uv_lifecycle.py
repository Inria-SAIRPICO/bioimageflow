from __future__ import annotations

import contextlib
import hashlib
import importlib.metadata
import io
import json
import re
import runpy
import sys
import uuid
import zipfile
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

import bioimageflow.cluster._gateway_run_state as gateway_run_module
from bioimageflow.cluster.gateway import GatewayState, handle_request
from bioimageflow.cluster.plan import RemoteExecutionPlan
from bioimageflow.cluster.preparation import prepare_cluster_invocation
from bioimageflow.cluster.protocol import GatewayRequest
from bioimageflow.cluster.reports import ClusterValidationReport
from bioimageflow.cluster.values import SchedulerJob
from bioimageflow.launcher.cluster_bundle import _manifest
from bioimageflow.launcher.schemas import new_run_id
from bioimageflow.parsl import ParslTaskPolicy
from bioimageflow.storage import canonical_json_bytes
from bioimageflow.workflow import Workflow


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


def _request(
    state: GatewayState,
    operation: str,
    arguments: dict[str, Any],
    *,
    operation_id: str | None = None,
    payload_digest: str | None = None,
):
    return handle_request(
        state,
        GatewayRequest.create(
            operation,
            arguments,
            operation_id=operation_id,
            payload_digest=payload_digest,
        ),
    )


def _upload_object(state: GatewayState, content: bytes, *, kind: str) -> str:
    digest = f"sha256:{hashlib.sha256(content).hexdigest()}"
    allocated = _request(
        state,
        "allocate_upload",
        {"size": len(content), "digest": digest, "kind": kind},
        operation_id=str(uuid.uuid4()),
        payload_digest=digest,
    )
    assert allocated.status == "ok"
    upload = Path(allocated.payload["upload_path"])
    upload.write_bytes(content)
    upload.chmod(0o600)
    committed = _request(
        state,
        "commit_upload",
        {
            "upload_token": allocated.payload["upload_token"],
            "size": len(content),
            "digest": digest,
        },
        operation_id=str(uuid.uuid4()),
        payload_digest=digest,
    )
    assert committed.status == "ok"
    return digest


def _installed_distributions() -> dict[str, str]:
    packages: dict[str, str] = {}
    for distribution in importlib.metadata.distributions():
        name = distribution.metadata.get("Name")
        if name:
            canonical = "-".join(filter(None, re.split(r"[-_.]+", name.lower())))
            previous = packages.setdefault(canonical, distribution.version)
            assert previous == distribution.version
    return dict(sorted(packages.items()))


def _fake_uv_wheel(path: Path, version: str) -> bytes:
    script = f"""#!/bin/sh
set -eu
if [ "$1" = "--version" ]; then
    echo "uv {version}"
    exit 0
fi
if [ "$1" = "venv" ]; then
    python=""
    previous=""
    target=""
    for argument in "$@"; do
        if [ "$previous" = "--python" ]; then python="$argument"; fi
        previous="$argument"
        target="$argument"
    done
    mkdir -p "$target/bin"
    printf '#!/bin/sh\nexec "%s" "$@"\n' "$python" > "$target/bin/python"
    chmod 700 "$target/bin/python"
    exit 0
fi
if [ "$1" = "pip" ]; then exit 0; fi
exit 64
""".encode()
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        member = zipfile.ZipInfo("uv/uv")
        member.external_attr = 0o755 << 16
        archive.writestr(member, script)
    return path.read_bytes()


def test_managed_uv_publish_validate_and_submit_uses_bound_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    psij = pytest.importorskip("psij")
    if "slurm" not in psij.JobExecutor.get_executor_names():
        pytest.skip("The managed-uv lifecycle fixture requires the PSI/J Slurm plugin.")

    content_root = tmp_path / "deployment-content"
    packages = _installed_distributions()
    bioimageflow_version = packages.pop("bioimageflow")
    uv_version = "0.0.1"
    uv_source = tmp_path / "uv.whl"
    uv_artifact = _artifact(
        content_root,
        "environment/installers/0/uv-0.0.1-py3-none-any.whl",
        _fake_uv_wheel(uv_source, uv_version),
    )
    bioimageflow_artifact = _artifact(
        content_root,
        f"environment/artifacts/bioimageflow/bioimageflow-{bioimageflow_version}-py3-none-any.whl",
        b"fixture-bioimageflow-wheel",
    )
    locked_packages: list[dict[str, Any]] = []
    for index, (name, version) in enumerate(packages.items()):
        filename = f"{name.replace('-', '_')}-{version}-py3-none-any.whl"
        artifact = _artifact(
            content_root,
            f"environment/registry/{index}/{filename}",
            f"fixture:{name}:{version}".encode(),
        )
        locked_packages.append(
            {
                "name": name,
                "version": version,
                "source_kind": "registry",
                "artifacts": [artifact],
            }
        )
    expected_packages = {"bioimageflow": bioimageflow_version, **packages}
    environment_plan = {
        "schema": "bioimageflow.uv_install_plan.v1",
        "installer": {
            "name": "uv",
            "version": uv_version,
            "artifacts": [uv_artifact],
        },
        "frozen": True,
        "network_resolution": False,
        "target_policy": "captured-wheels-target-selected",
        "requires_python": (
            f">={sys.version_info.major}.{sys.version_info.minor},"
            f"<{sys.version_info.major}.{sys.version_info.minor + 1}"
        ),
        "local_artifacts": [
            {
                "name": "bioimageflow",
                "version": bioimageflow_version,
                "wheel": bioimageflow_artifact,
            }
        ],
        "locked_packages": locked_packages,
        "required_distribution_names": sorted(expected_packages),
        "psij_scheduler_plugin": {
            "scheduler": "slurm",
            "distribution": "psij-python",
            "version": expected_packages["psij-python"],
        },
    }
    factory = content_root / "parsl" / "factory.py"
    factory.parent.mkdir(parents=True)
    factory.write_text(
        """from parsl import Config
from parsl.executors import ThreadPoolExecutor
from bioimageflow.parsl.startup import CORE_REQUIREMENT
from bioimageflow.parsl import (
    ExecutorBinding,
    ExecutorCapabilities,
    ParslFactoryResult,
    WorkerEnvironmentAttestation,
    WorkerSlotCapacity,
)


def build(runtime):
    binding = ExecutorBinding(
        label="cpu",
        environments=(WorkerEnvironmentAttestation(
            name="managed-uv",
            dependency_hash="a" * 64,
            allow_flexible_versions=False,
            core_requirement=CORE_REQUIREMENT,
        ),),
        capabilities=ExecutorCapabilities(
            storage_modes=("shared_fs",),
            tool_origin_modes=("installed_module",),
            slot=WorkerSlotCapacity(cpu=1),
        ),
    )
    return ParslFactoryResult(
        config=Config(executors=[ThreadPoolExecutor(label="cpu")], retries=0),
        executor_bindings={"cpu": binding},
    )
""",
        encoding="utf-8",
    )
    plan_path = content_root / "environment" / "install-plan.json"
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_bytes(canonical_json_bytes(environment_plan))
    content_entries = _manifest(content_root)["entries"]
    identity = {
        "schema": "bioimageflow.cluster_deployment_manifest.v1",
        "environment": {
            "schema": "bioimageflow.cluster_environment.v1",
            "kind": "uv",
            "source": None,
            "groups": [],
            "extras": [],
            "package": None,
            "environment": None,
            "auth_refs": {},
        },
        "environment_ownership": "content",
        "environment_plan": environment_plan,
        "setup": None,
        "parsl": {
            "schema": "bioimageflow.parsl_configuration.v1",
            "source_kind": "file",
            "source": None,
            "factory": "build",
            "kwargs": {},
            "secret_refs": {},
            "include_count": 0,
        },
        "scheduler": "slurm",
        "bioimageflow_version": bioimageflow_version,
        "content_entries": content_entries,
        "gateway_protocol": 1,
        "factory_runtime_contract": 1,
    }
    deployment_id = f"sha256:{hashlib.sha256(canonical_json_bytes(identity)).hexdigest()}"
    manifest = {
        **identity,
        "deployment_id": deployment_id,
        "manifest_digest": deployment_id,
    }
    (content_root / "deployment-manifest.json").write_bytes(
        canonical_json_bytes(manifest)
    )
    deployment_archive = tmp_path / "deployment.zip"
    with zipfile.ZipFile(
        deployment_archive, "w", compression=zipfile.ZIP_DEFLATED
    ) as archive:
        for source in sorted(content_root.rglob("*")):
            if source.is_file():
                archive.write(source, source.relative_to(content_root).as_posix())

    state = GatewayState.initialize(tmp_path / "cluster-root")
    deployment_object = _upload_object(
        state, deployment_archive.read_bytes(), kind="deployment"
    )
    published = _request(
        state,
        "publish_deployment",
        {
            "object_id": deployment_object,
            "deployment_id": deployment_id,
            "manifest_digest": deployment_id,
        },
        operation_id=str(uuid.uuid4()),
        payload_digest=deployment_object,
    )
    assert published.status == "ok", published.to_dict()
    final_deployment_id = published.payload["deployment_id"]
    deployment = state.root / "deployments" / final_deployment_id[7:]
    runtime_python = deployment / "environment" / "bin" / "python"
    assert runtime_python.is_file()

    scheduler_job = SchedulerJob("slurm", timedelta(minutes=5))
    validated = _request(
        state,
        "validate-deployment",
        {
            "deployment_id": final_deployment_id,
            "scheduler_job": scheduler_job.to_dict(),
            "timeout": 30,
        },
    )
    assert validated.status == "ok", validated.to_dict()
    validation = ClusterValidationReport.from_dict(dict(validated.payload))
    assert validation.valid
    assert (
        validation.evidence["environment_attestation_digest"]
        == published.payload["environment_attestation_digest"]
    )

    with prepare_cluster_invocation(Workflow(name="managed-uv-lifecycle")) as prepared:
        invocation_archive = tmp_path / "invocation.zip"
        with zipfile.ZipFile(
            invocation_archive, "w", compression=zipfile.ZIP_DEFLATED
        ) as archive:
            for source in sorted(prepared.root.rglob("*")):
                if source.is_file():
                    archive.write(source, source.relative_to(prepared.root).as_posix())
        invocation_object = _upload_object(
            state, invocation_archive.read_bytes(), kind="invocation"
        )
        execution_plan = RemoteExecutionPlan(
            host="cluster",
            cluster_root=str(state.root),
            attempt_id=str(uuid.uuid4()),
            run_id=new_run_id(),
            deployment_id=final_deployment_id,
            external_attestation_digest=None,
            invocation_digest=prepared.invocation_digest,
            validation_digest=validation.validation_digest,
            validation_expires_at=validation.expires_at,
            validation_evidence=validation.evidence,
            executor_claims=validation.executor_bindings,
            storage_path=str(tmp_path / "workflow-storage"),
            expires_at="2099-01-01T00:00:00Z",
            scheduler_job=scheduler_job,
            task_policy=ParslTaskPolicy(),
            nodes=(),
        )
        observed: dict[str, Any] = {}
        original_child = gateway_run_module._run_json_child

        def run_child(argv: list[str], **kwargs: Any) -> dict[str, Any]:
            if Path(argv[3]).name != "submit_run.py":
                return original_child(argv, **kwargs)
            observed["executable"] = argv[0]
            request = json.loads(Path(argv[4]).read_bytes())
            observed["pre_launch"] = request["pre_launch"]
            import bioimageflow.launcher.backends as backends

            monkeypatch.setattr(
                backends, "launch_orchestrator", lambda *_args, **_kwargs: None
            )
            previous_argv = sys.argv
            previous_path = list(sys.path)
            output = io.StringIO()
            try:
                sys.argv = [argv[3], argv[4]]
                with contextlib.redirect_stdout(output):
                    runpy.run_path(argv[3], run_name="__main__")
            finally:
                sys.argv = previous_argv
                sys.path[:] = previous_path
            return json.loads(output.getvalue())

        monkeypatch.setattr(gateway_run_module, "_run_json_child", run_child)
        submitted = _request(
            state,
            "submit-plan",
            {
                "plan": execution_plan.to_dict(),
                "invocation_manifest": prepared.to_dict(),
                "object_id": invocation_object,
                "object_size": invocation_archive.stat().st_size,
            },
            operation_id=execution_plan.attempt_id,
            payload_digest=invocation_object,
        )

    assert submitted.status == "ok", submitted.to_dict()
    assert submitted.payload["run_id"] == execution_plan.run_id
    assert observed["executable"] == str(runtime_python)
    assert f". {deployment / 'activation.sh'}" in observed["pre_launch"]
