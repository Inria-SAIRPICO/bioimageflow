from __future__ import annotations

from pathlib import PurePosixPath
from unittest.mock import patch

import pytest

from bioimageflow.parsl.factory import ParslFactoryRuntime
from bioimageflow.parsl.managed_validation import validate_managed_factory
from bioimageflow.parsl.providers import (
    ManagedProviderError,
    validate_managed_provider,
)


def _runtime() -> ParslFactoryRuntime:
    return ParslFactoryRuntime(
        deployment_root=PurePosixPath("/shared/bioimageflow/deployments/example"),
        deployment_id="sha256:" + "a" * 64,
        worker_init="set -eu\n. /shared/activate\n",
        environment_name="managed",
        environment_identity="sha256:" + "b" * 64,
        core_requirement=">=0.3,<0.4",
    )


@pytest.mark.parametrize(
    ("provider_name", "scheduler", "adapter"),
    [
        ("SlurmProvider", "slurm", "parsl-htex-slurm.v1"),
        ("PBSProProvider", "pbs", "parsl-htex-pbspro.v1"),
        ("TorqueProvider", "pbs", "parsl-htex-torque.v1"),
        ("LSFProvider", "lsf", "parsl-htex-lsf.v1"),
    ],
)
def test_public_provider_adapters_verify_exact_worker_init(
    provider_name: str,
    scheduler: str,
    adapter: str,
) -> None:
    from parsl.executors import HighThroughputExecutor
    from parsl import providers

    provider_type = getattr(providers, provider_name)
    context = (
        patch.object(provider_type, "execute_wait", return_value=(1, "", ""))
        if provider_name == "SlurmProvider"
        else patch.object(provider_type, "submit", provider_type.submit)
    )
    with context:
        provider = provider_type(worker_init=_runtime().worker_init, init_blocks=0)
    executor = HighThroughputExecutor(label="cpu", provider=provider)

    evidence = validate_managed_provider(
        executor,
        worker_init=_runtime().worker_init,
        orchestrator_scheduler=scheduler,
    )

    assert evidence.adapter == adapter
    assert evidence.worker_init_verified
    assert evidence.shared_root == "runtime-unverified"
    assert evidence.nested_submission == "runtime-unverified"
    assert "scheduler_options" not in evidence.settings["provider"]


def test_provider_adapter_rejects_scheduler_and_worker_init_drift() -> None:
    from parsl.executors import HighThroughputExecutor
    from parsl.providers import SlurmProvider

    with patch.object(SlurmProvider, "execute_wait", return_value=(1, "", "")):
        executor = HighThroughputExecutor(
            label="cpu", provider=SlurmProvider(worker_init="wrong", init_blocks=0)
        )

    with pytest.raises(ManagedProviderError, match="incompatible"):
        validate_managed_provider(
            executor,
            worker_init=_runtime().worker_init,
            orchestrator_scheduler="pbs",
        )
    with pytest.raises(ManagedProviderError, match="exact generated"):
        validate_managed_provider(
            executor,
            worker_init=_runtime().worker_init,
            orchestrator_scheduler="slurm",
        )


def test_isolated_validation_returns_only_normalized_facts() -> None:
    report = validate_managed_factory(
        "tests.testkit.managed_factories:valid",
        runtime=_runtime(),
        orchestrator_scheduler="slurm",
    )

    assert report.valid
    assert report.retries == 0
    assert report.executor_labels == ("cpu",)
    assert report.executor_bindings["cpu"]["label"] == "cpu"
    assert report.provider_evidence[0]["adapter"] == "parsl-htex-slurm.v1"
    assert "config" not in report.to_dict()
    assert "dfk" not in repr(report).lower()


@pytest.mark.parametrize(
    ("factory", "category"),
    [
        ("retries", "parsl-retries-enabled"),
        ("label_mismatch", "executor-label-mismatch"),
        ("wrong_worker_init", "worker-initialization-missing"),
        ("wrong_result", "parsl-factory-failed"),
        ("forbidden_dfk", "parsl-factory-failed"),
    ],
)
def test_isolated_validation_rejects_invalid_factory_contract(
    factory: str,
    category: str,
) -> None:
    report = validate_managed_factory(
        f"tests.testkit.managed_factories:{factory}",
        runtime=_runtime(),
        orchestrator_scheduler="slurm",
    )

    assert not report.valid
    assert report.diagnostics[0].category == category
    assert report.executor_bindings == {}


def test_secret_values_use_private_handoff_and_never_enter_report(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "literal-secret-value"
    monkeypatch.setenv("CLUSTER_CREDENTIAL", secret)

    report = validate_managed_factory(
        "tests.testkit.managed_factories:secret_failure",
        runtime=_runtime(),
        orchestrator_scheduler="slurm",
        secret_refs={"credential": "CLUSTER_CREDENTIAL"},
    )

    assert not report.valid
    assert secret not in repr(report)
    assert secret not in str(report.to_dict())
    assert "resolved secrets were present" in report.diagnostics[0].message


def test_missing_secret_names_only_the_reference() -> None:
    report = validate_managed_factory(
        "tests.testkit.managed_factories:secret_failure",
        runtime=_runtime(),
        orchestrator_scheduler="slurm",
        secret_refs={"credential": "ABSENT_REFERENCE"},
        secret_values={},
    )

    assert not report.valid
    assert report.diagnostics[0].category == "secret-reference-missing"
    assert "ABSENT_REFERENCE" in report.diagnostics[0].message


def test_timeout_terminates_child_and_removes_private_state(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    private_root = tmp_path / "factory-private"

    def make_private_root(*, prefix: str) -> str:
        assert prefix == "bioimageflow-factory-"
        private_root.mkdir()
        return str(private_root)

    monkeypatch.setattr(
        "bioimageflow.parsl.managed_validation.tempfile.mkdtemp",
        make_private_root,
    )
    report = validate_managed_factory(
        "tests.testkit.managed_factories:slow",
        runtime=_runtime(),
        orchestrator_scheduler="slurm",
        timeout=0.1,
    )

    assert not report.valid
    assert "timed out" in report.diagnostics[0].message
    assert not private_root.exists()
