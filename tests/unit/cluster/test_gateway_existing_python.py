from __future__ import annotations

import hashlib
import json
import sys
import uuid
import zipfile
from datetime import timedelta
from pathlib import Path

import pytest

import bioimageflow.cluster.gateway as gateway_module
from bioimageflow.cluster.deployment import prepare_deployment
from bioimageflow.cluster.gateway import GatewayState, handle_request
from bioimageflow.cluster.protocol import GatewayRequest
from bioimageflow.cluster.reports import ClusterValidationReport
from bioimageflow.cluster.values import (
    ClusterEnvironment,
    ParslConfiguration,
    RemoteClusterConfig,
    SchedulerJob,
)


def _request(
    state: GatewayState,
    operation: str,
    arguments: dict,
    *,
    payload_digest: str | None = None,
):
    return handle_request(
        state,
        GatewayRequest.create(
            operation,
            arguments,
            operation_id=str(uuid.uuid4()) if payload_digest is not None else None,
            payload_digest=payload_digest,
        ),
    )


def test_existing_python_publication_and_factory_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    psij = pytest.importorskip("psij")
    if "slurm" not in psij.JobExecutor.get_executor_names():
        pytest.skip("The existing-Python fixture requires the PSI/J Slurm plugin.")
    monkeypatch.setenv("BIF_FACTORY_CREDENTIAL", "private-value")
    factory = tmp_path / "factory.py"
    factory.write_text(
        """from parsl import Config
from parsl.executors import ThreadPoolExecutor
from bioimageflow.parsl import (
    ExecutorBinding,
    ExecutorCapabilities,
    ParslFactoryResult,
    WorkerEnvironmentAttestation,
    WorkerSlotCapacity,
)


def build(runtime, credential):
    assert credential == "private-value"
    binding = ExecutorBinding(
        label="cpu",
        environments=(WorkerEnvironmentAttestation(
            name="site-external",
            dependency_hash="a" * 64,
            allow_flexible_versions=False,
            core_requirement="bioimageflow-core>=0.3.0,<0.4",
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
    config = RemoteClusterConfig(
        "login.example",
        "/cluster/alice/bioimageflow",
        environment=ClusterEnvironment.from_existing_python(sys.executable),
        parsl=ParslConfiguration.from_file(
            factory, secret_refs={"credential": "BIF_FACTORY_CREDENTIAL"}
        ),
        orchestrator=SchedulerJob("slurm", timedelta(minutes=10)),
    )
    state = GatewayState.initialize(tmp_path / "cluster-root")
    with prepare_deployment(config) as prepared:
        archive = tmp_path / "deployment.zip"
        with zipfile.ZipFile(archive, "x", compression=zipfile.ZIP_DEFLATED) as output:
            for source in sorted(prepared.root.rglob("*")):
                if source.is_file():
                    output.write(source, source.relative_to(prepared.root).as_posix())
        content = archive.read_bytes()
        object_id = f"sha256:{hashlib.sha256(content).hexdigest()}"
        allocated = _request(
            state,
            "allocate_upload",
            {"size": len(content), "digest": object_id, "kind": "deployment"},
            payload_digest=object_id,
        ).payload
        upload = Path(allocated["upload_path"])
        upload.write_bytes(content)
        upload.chmod(0o600)
        committed = _request(
            state,
            "commit_upload",
            {
                "upload_token": allocated["upload_token"],
                "size": len(content),
                "digest": object_id,
            },
            payload_digest=object_id,
        )
        assert committed.status == "ok"
        published = _request(
            state,
            "publish_deployment",
            {
                "object_id": object_id,
                "deployment_id": prepared.deployment_id,
                "manifest_digest": prepared.manifest["manifest_digest"],
            },
            payload_digest=object_id,
        )

        assert published.status == "ok", published.to_dict()
        final_deployment_id = published.payload["deployment_id"]
        assert final_deployment_id != prepared.deployment_id
        assert published.payload["environment_installed"] is True
        assert published.payload["external_attestation_digest"].startswith("sha256:")
        reused = _request(
            state,
            "publish_deployment",
            {
                "object_id": object_id,
                "deployment_id": prepared.deployment_id,
                "manifest_digest": prepared.manifest["manifest_digest"],
            },
            payload_digest=object_id,
        )
        assert reused.payload["deployment_id"] == final_deployment_id
        assert reused.payload["reused"] is True
        validation = _request(
            state,
            "validate-deployment",
            {
                "deployment_id": final_deployment_id,
                "scheduler_job": config.orchestrator.to_dict(),
                "timeout": 30,
            },
        )

    assert validation.status == "ok", json.dumps(validation.to_dict(), indent=2)
    report = ClusterValidationReport.from_dict(dict(validation.payload))
    assert report.valid
    assert set(report.executor_bindings) == {"cpu"}
    assert (
        report.evidence["external_attestation_digest"]
        == published.payload["external_attestation_digest"]
    )

    original_attestation = gateway_module._attest_existing_python

    def changed_attestation(*args, **kwargs):
        attestation, _digest = original_attestation(*args, **kwargs)
        return attestation, "sha256:" + "f" * 64

    monkeypatch.setattr(
        gateway_module, "_attest_existing_python", changed_attestation
    )
    drifted = _request(
        state,
        "validate-deployment",
        {
            "deployment_id": final_deployment_id,
            "scheduler_job": config.orchestrator.to_dict(),
            "timeout": 30,
        },
    )
    assert drifted.status == "error"
    assert drifted.diagnostic["category"] == "external-environment-changed"
