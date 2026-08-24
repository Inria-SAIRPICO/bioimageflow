from __future__ import annotations

import json
import os
from dataclasses import FrozenInstanceError
from datetime import timedelta
from pathlib import PurePosixPath

import pytest

from bioimageflow.cluster.reports import (
    CLUSTER_DIAGNOSTIC_CATEGORIES,
    ClusterConnectionReport,
    ClusterDeployment,
    ClusterDiagnostic,
    ClusterValidationReport,
)
from bioimageflow.cluster.values import (
    ClusterEnvironment,
    ParslConfiguration,
    RemoteClusterConfig,
    SchedulerJob,
    SetupScript,
)


_DIGEST = "sha256:" + "a" * 64


@pytest.mark.parametrize(
    ("value", "loader"),
    [
        (SetupScript.from_text("module load Python/3.12\n"), SetupScript.from_dict),
        (
            ClusterEnvironment.from_existing_python("/opt/python/bin/python3"),
            ClusterEnvironment.from_dict,
        ),
        (
            SchedulerJob(
                "slurm",
                timedelta(hours=2),
                queue="compute",
                cpu=2,
                memory="4 GiB",
                attributes={"reservation": "science", "exclusive": True},
            ),
            SchedulerJob.from_dict,
        ),
        (
            ParslConfiguration.from_module(
                "site.parsl:build",
                kwargs={"workers": 2, "labels": ["cpu"]},
                secret_refs={"credential": "PARSL_CREDENTIAL"},
            ),
            ParslConfiguration.from_dict,
        ),
        (
            RemoteClusterConfig(
                "alice@login.example",
                PurePosixPath("/cluster/alice/bioimageflow"),
            ),
            RemoteClusterConfig.from_dict,
        ),
        (
            ClusterDiagnostic(
                "validation",
                "unsupported-managed-provider",
                "The provider is not supported.",
            ),
            ClusterDiagnostic.from_dict,
        ),
        (
            ClusterConnectionReport(True, True, False, "1.0", (1,)),
            ClusterConnectionReport.from_dict,
        ),
        (
            ClusterDeployment(
                _DIGEST,
                _DIGEST,
                "content",
                False,
                "uv",
                "gateway-v1",
            ),
            ClusterDeployment.from_dict,
        ),
        (
            ClusterValidationReport(
                _DIGEST,
                True,
                None,
                "2026-08-24T12:00:00Z",
                executor_bindings={"cpu": {"cores": 4}},
            ),
            ClusterValidationReport.from_dict,
        ),
    ],
)
def test_public_values_have_strict_json_round_trips(
    value: object, loader: object
) -> None:
    payload = value.to_dict()  # type: ignore[attr-defined]

    assert json.loads(json.dumps(payload)) == payload
    assert loader(payload).to_dict() == payload  # type: ignore[operator]
    with pytest.raises(ValueError):
        loader({**payload, "unexpected": True})  # type: ignore[operator]


def test_public_configuration_values_are_frozen_and_deeply_immutable() -> None:
    environment = ClusterEnvironment.from_uv_project(
        ".",
        groups=("analysis",),
        auth_refs={"index": "PRIVATE_INDEX"},
    )
    scheduler = SchedulerJob(
        "slurm",
        timedelta(minutes=30),
        attributes={"constraint": "gpu"},
    )
    parsl = ParslConfiguration.from_module(
        "site.parsl:build",
        kwargs={"routes": ["cpu"]},
    )

    with pytest.raises(FrozenInstanceError):
        environment.kind = "pixi"  # type: ignore[misc]
    with pytest.raises(TypeError):
        environment.auth_refs["index"] = "OTHER"  # type: ignore[index]
    with pytest.raises(TypeError):
        scheduler.attributes["constraint"] = "cpu"  # type: ignore[index]
    with pytest.raises(TypeError):
        parsl.kwargs["routes"] = []  # type: ignore[index]


def test_setup_script_preserves_bytes_and_redacts_source(tmp_path) -> None:
    source = tmp_path / "sensitive-setup-name.sh"
    content = b"module load Python/3.12\r\nexport MODE=test"
    source.write_bytes(content)

    setup = SetupScript.from_file(source)

    assert setup.content == content
    assert str(source) not in repr(setup)
    assert content.decode() not in repr(setup)
    assert str(source) not in json.dumps(setup.to_dict())
    detached = SetupScript.from_dict(setup.to_dict())
    with pytest.raises(RuntimeError, match="local-state-unavailable"):
        _ = detached.content


def test_setup_script_rejects_unsafe_local_sources(tmp_path) -> None:
    target = tmp_path / "setup.sh"
    target.write_text("module load Python\n", encoding="utf-8")
    symlink = tmp_path / "setup-link.sh"
    symlink.symlink_to(target)
    fifo = tmp_path / "setup.fifo"
    os.mkfifo(fifo)

    for source in (symlink, fifo):
        with pytest.raises(ValueError, match="regular non-symlink file"):
            SetupScript.from_file(source)

    with pytest.raises(ValueError, match="NUL"):
        SetupScript.from_text("echo before\x00echo after")
    with pytest.raises(ValueError, match="64 KiB"):
        SetupScript.from_text("x" * (SetupScript.MAX_BYTES + 1))


@pytest.mark.parametrize(
    "path",
    ["relative/setup.sh", "/cluster/../setup.sh", "//cluster/setup.sh"],
)
def test_cluster_setup_script_requires_a_normalized_absolute_path(path: str) -> None:
    with pytest.raises(ValueError, match="normalized absolute"):
        SetupScript.from_cluster_file(path, sha256=_DIGEST)


def test_cluster_setup_script_has_unknown_size_until_remote_verification() -> None:
    setup = SetupScript.from_cluster_file("/cluster/setup.sh", sha256=_DIGEST)

    assert setup.size is None
    assert setup.to_dict()["size"] is None
    assert SetupScript.from_dict(setup.to_dict()) == setup


def test_scheduler_values_normalize_resources_and_reject_shell_fragments() -> None:
    job = SchedulerJob("slurm", timedelta(seconds=90), memory="1536 MiB")

    assert job.to_dict()["memory_bytes"] == 1610612736
    assert job.to_dict()["walltime_seconds"] == 90
    with pytest.raises(ValueError, match="safe scheduler identifier"):
        SchedulerJob("slurm", timedelta(minutes=5), queue="compute; touch /tmp/x")
    with pytest.raises(ValueError, match="scalar"):
        SchedulerJob(
            "slurm",
            timedelta(minutes=5),
            attributes={"native": {"directive": "--exclusive"}},
        )
    with pytest.raises(ValueError, match="whole seconds"):
        SchedulerJob("slurm", timedelta(microseconds=1))
    with pytest.raises(TypeError, match="memory"):
        SchedulerJob("slurm", timedelta(minutes=5), memory=True)  # type: ignore[arg-type]


def test_secret_bearing_json_values_are_rejected() -> None:
    with pytest.raises(ValueError, match="secret-bearing"):
        ParslConfiguration.from_module(
            "site.parsl:build",
            kwargs={"api_token": "do-not-store"},
        )
    with pytest.raises(ValueError, match="kwargs and secret_refs"):
        ParslConfiguration.from_module(
            "site.parsl:build",
            kwargs={"workers": 2},
            secret_refs={"workers": "PARSL_WORKERS"},
        )


@pytest.mark.parametrize(
    "payload_update",
    [
        {"kind": "uv", "environment": "default"},
        {"kind": "pixi", "groups": ["analysis"], "environment": "default"},
        {"kind": "pylock", "package": "example"},
        {"kind": "wheelhouse", "auth_refs": {"index": "INDEX_TOKEN"}},
    ],
)
def test_cluster_environment_rejects_fields_for_another_adapter(
    tmp_path,
    payload_update: dict[str, object],
) -> None:
    lock = tmp_path / "pylock.toml"
    lock.write_text('lock-version = "1.0"\n', encoding="utf-8")
    payload = ClusterEnvironment.from_pylock(lock).to_dict()
    payload.update(payload_update)
    if payload["kind"] == "uv":
        payload.update({"source": str(tmp_path), "lock": None})
    elif payload["kind"] == "pixi":
        payload.update({"source": str(tmp_path), "lock": None})
    elif payload["kind"] == "wheelhouse":
        payload.update({"source": str(tmp_path), "lock": str(lock)})

    with pytest.raises(ValueError):
        ClusterEnvironment.from_dict(payload)


def test_cluster_environment_round_trip_rejects_non_array_selections(tmp_path) -> None:
    payload = ClusterEnvironment.from_uv_project(tmp_path).to_dict()
    payload["groups"] = "analysis"

    with pytest.raises(ValueError, match="arrays"):
        ClusterEnvironment.from_dict(payload)


@pytest.mark.parametrize("value", [" default", "default ", "bad\nname"])
def test_environment_selection_names_are_trimmed_and_control_free(
    tmp_path,
    value: str,
) -> None:
    with pytest.raises(ValueError):
        ClusterEnvironment.from_pixi_project(tmp_path, environment=value)


def test_normative_diagnostic_categories_are_registered() -> None:
    assert {
        "deployment-install-failed",
        "environment-installer-unavailable",
        "parsl-factory-failed",
        "submission-uncertain",
        "result-integrity-failed",
        "cleanup-conflict",
    } <= CLUSTER_DIAGNOSTIC_CATEGORIES


def test_reports_reject_inconsistent_or_malformed_state() -> None:
    with pytest.raises(ValueError, match="available gateway"):
        ClusterConnectionReport(False, True, False)
    with pytest.raises(TypeError, match="boolean"):
        ClusterValidationReport(
            _DIGEST,
            1,  # type: ignore[arg-type]
            None,
            "2026-08-24T12:00:00Z",
        )
    with pytest.raises(ValueError, match="RFC 3339"):
        ClusterValidationReport(_DIGEST, True, None, "tomorrow")


def test_validation_report_digest_rejects_content_tampering() -> None:
    report = ClusterValidationReport(
        _DIGEST,
        True,
        None,
        "2999-08-24T12:00:00Z",
        executor_bindings={"cpu": {"cores": 4}},
    )
    payload = report.to_dict()
    payload["valid"] = False

    with pytest.raises(ValueError, match="digest mismatch"):
        ClusterValidationReport.from_dict(payload)


@pytest.mark.parametrize("timeout", [0, float("nan"), float("inf"), 601])
def test_cluster_connection_timeout_is_bounded(timeout: float) -> None:
    with pytest.raises(ValueError, match="connect_timeout"):
        RemoteClusterConfig(
            "login.example",
            PurePosixPath("/cluster/alice/bioimageflow"),
            connect_timeout=timeout,
        )
