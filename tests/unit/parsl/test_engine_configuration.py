"""Local-only Parsl engine constructor contracts."""

from __future__ import annotations

from pathlib import Path
from types import MappingProxyType

import pytest

from bioimageflow import (
    ExecutorBinding,
    ExecutorCapabilities,
    ParslEngine,
    ParslTaskError,
    ParslTaskPolicy,
    ResourceLifetime,
    WorkerEnvironmentAttestation,
    WorkerSlotCapacity,
    WorkerTaskError,
)


def _binding(label: str = "cpu") -> ExecutorBinding:
    return ExecutorBinding(
        label=label,
        environments=(
            WorkerEnvironmentAttestation(
                name="analysis",
                dependency_hash="d" * 64,
                allow_flexible_versions=False,
                core_requirement="bioimageflow-core>=0.1.7,<0.2",
            ),
        ),
        capabilities=ExecutorCapabilities(
            storage_modes=("shared_fs",),
            tool_origin_modes=("installed_module",),
            slot=WorkerSlotCapacity(cpu=2),
        ),
    )


def test_constructor_normalizes_local_configuration(tmp_path: Path) -> None:
    config = object()
    policy = ParslTaskPolicy(row_chunk_size=4, max_in_flight=8)
    engine = ParslEngine(
        parsl_config=config,
        executor_bindings={"cpu": _binding()},
        node_routes={"workflow/node": "cpu"},
        environment_routes={"analysis:identity": "cpu"},
        shared_runtime_root=tmp_path / ".." / tmp_path.name,
        execution="parallel",
        task_policy=policy,
        resource_lifetime="engine",
    )

    assert engine.parsl_config is config
    assert engine.dfk is None
    assert engine.executor_bindings == {"cpu": _binding()}
    assert isinstance(engine.executor_bindings, MappingProxyType)
    assert engine.node_routes == {"workflow/node": "cpu"}
    assert isinstance(engine.node_routes, MappingProxyType)
    assert engine.environment_routes == {"analysis:identity": "cpu"}
    assert engine.shared_runtime_root == tmp_path.resolve()
    assert engine.execution == "parallel"
    assert engine.storage_mode == "shared_fs"
    assert engine.task_policy is policy
    assert engine.resource_lifetime is ResourceLifetime.ENGINE


def test_injected_dfk_requires_external_lifetime() -> None:
    dfk = object()
    engine = ParslEngine(
        dfk=dfk,
        executor_bindings={"cpu": _binding()},
        resource_lifetime="external",
    )

    assert engine.dfk is dfk
    assert engine.parsl_config is None

    with pytest.raises(ValueError, match="requires resource_lifetime='external'"):
        ParslEngine(dfk=dfk, executor_bindings={"cpu": _binding()})

    with pytest.raises(ValueError, match="requires an injected dfk"):
        ParslEngine(
            parsl_config=object(),
            executor_bindings={"cpu": _binding()},
            resource_lifetime="external",
        )


@pytest.mark.parametrize(
    ("parsl_config", "dfk"),
    [(None, None), (object(), object())],
)
def test_constructor_requires_exactly_one_runtime_owner(
    parsl_config: object | None,
    dfk: object | None,
) -> None:
    with pytest.raises(ValueError, match="Exactly one"):
        ParslEngine(
            parsl_config=parsl_config,
            dfk=dfk,
            executor_bindings={"cpu": _binding()},
        )


def test_constructor_rejects_staged_storage() -> None:
    with pytest.raises(ValueError, match="unavailable"):
        ParslEngine(
            parsl_config=object(),
            executor_bindings={"cpu": _binding()},
            storage_mode="staged",
        )


def test_constructor_rejects_binding_key_mismatch_and_unknown_routes() -> None:
    with pytest.raises(ValueError, match="does not match"):
        ParslEngine(
            parsl_config=object(),
            executor_bindings={"other": _binding()},
        )

    with pytest.raises(ValueError, match="unknown executor"):
        ParslEngine(
            parsl_config=object(),
            executor_bindings={"cpu": _binding()},
            node_routes={"node": "missing"},
        )


def test_constructor_rejects_unknown_local_values() -> None:
    with pytest.raises(ValueError, match="execution"):
        ParslEngine(
            parsl_config=object(),
            executor_bindings={"cpu": _binding()},
            execution="serial",
        )

    with pytest.raises(ValueError, match="resource_lifetime"):
        ParslEngine(
            parsl_config=object(),
            executor_bindings={"cpu": _binding()},
            resource_lifetime="forever",
        )

    with pytest.raises(TypeError, match="task_policy"):
        ParslEngine(
            parsl_config=object(),
            executor_bindings={"cpu": _binding()},
            task_policy={"max_in_flight": 4},
        )

    with pytest.raises(ValueError, match="shared_runtime_root"):
        ParslEngine(
            parsl_config=object(),
            executor_bindings={"cpu": _binding()},
            shared_runtime_root="",
        )


def test_context_manager_closes_shell_idempotently() -> None:
    engine = ParslEngine(
        parsl_config=object(),
        executor_bindings={"cpu": _binding()},
    )

    with engine as active:
        assert active is engine

    engine.close()
    with pytest.raises(RuntimeError, match="closed"):
        engine.__enter__()


def test_parsl_task_error_is_structured_worker_error() -> None:
    error = ParslTaskError(
        scoped_node_name="nested/tool",
        tool_origin={"mode": "installed_module", "module": "tools"},
        executor_label="cpu",
        task_id="task_0000000000000001",
        invocation_id="inv_" + "1" * 32,
        cache_attempt_id="att_" + "2" * 32,
        task_retry=0,
        row_position=(2, 4),
        original_type="RuntimeError",
        original_message="worker failed",
        remote_traceback="trace",
    )

    assert isinstance(error, WorkerTaskError)
    assert error.node_name == "nested/tool"
    assert error.executor_label == "cpu"
    assert error.row_position == (2, 4)
    assert error.original_type == "RuntimeError"
    assert "worker failed" in str(error)
    assert "trace" in str(error)
