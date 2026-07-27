"""Workflow factory validation and explicit Parsl engine precedence."""

from __future__ import annotations

from threading import Event, Thread
from types import SimpleNamespace
from typing import Any

import pandas as pd
import pytest

from bioimageflow import (
    ExecutorBinding,
    ExecutorCapabilities,
    ParslEngine,
    ParslTaskPolicy,
    ResourceLifetime,
    WorkerEnvironmentAttestation,
    WorkerSlotCapacity,
    Workflow,
)
from bioimageflow.parsl import engine as engine_module
from tests.testkit.runtime_cache import CountingTable


def _binding() -> ExecutorBinding:
    return ExecutorBinding(
        label="cpu",
        environments=(
            WorkerEnvironmentAttestation(
                name="analysis",
                dependency_hash="f" * 64,
                allow_flexible_versions=False,
                core_requirement="bioimageflow-core>=0.1.7,<0.2",
            ),
        ),
        capabilities=ExecutorCapabilities(
            storage_modes=("shared_fs",),
            tool_origin_modes=("installed_module",),
            slot=WorkerSlotCapacity(cpu=4),
        ),
    )


def test_parsl_factory_validates_and_forwards_exact_runtime_values(
    tmp_path,
) -> None:
    config = object()
    policy = ParslTaskPolicy(row_chunk_size=3, max_in_flight=7)
    workflow = Workflow(
        storage_path=tmp_path,
        engine="parsl",
        execution="sequential",
    )

    engine = workflow.create_engine(
        parsl_config=config,
        executor_bindings={"cpu": _binding()},
        parsl_node_routes={"nested/tool": "cpu"},
        parsl_environment_routes={"analysis:identity": "cpu"},
        parsl_shared_runtime_root=tmp_path,
        parsl_task_policy=policy,
        resource_lifetime="engine",
    )

    assert type(engine) is ParslEngine
    assert engine.parsl_config is config
    assert engine.dfk is None
    assert engine.executor_bindings == {"cpu": _binding()}
    assert engine.node_routes == {"nested/tool": "cpu"}
    assert engine.environment_routes == {"analysis:identity": "cpu"}
    assert engine.shared_runtime_root == tmp_path.resolve()
    assert engine.execution == "sequential"
    assert engine.task_policy is policy
    assert engine.resource_lifetime is ResourceLifetime.ENGINE


def test_parsl_factory_forwards_external_dfk(tmp_path) -> None:
    dfk = object()
    engine = Workflow(storage_path=tmp_path, engine="parsl").create_engine(
        dfk=dfk,
        executor_bindings={"cpu": _binding()},
        resource_lifetime="external",
        parsl_execution="parallel",
    )

    assert type(engine) is ParslEngine
    assert engine.dfk is dfk
    assert engine.execution == "parallel"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"parsl_config": object()},
        {"dfk": object()},
        {"executor_bindings": {}},
        {"parsl_node_routes": {}},
        {"parsl_environment_routes": {}},
        {"parsl_shared_runtime_root": "."},
        {"parsl_execution": "parallel"},
        {"parsl_task_policy": ParslTaskPolicy()},
    ],
)
@pytest.mark.parametrize("backend", ["direct", "wetlands"])
def test_non_parsl_factory_rejects_every_parsl_argument(
    backend: str,
    kwargs: dict[str, Any],
    tmp_path,
) -> None:
    with pytest.raises(ValueError, match="Parsl arguments"):
        Workflow(storage_path=tmp_path, engine=backend).create_engine(**kwargs)


def test_direct_factory_rejects_resource_ownership_and_manager(tmp_path) -> None:
    workflow = Workflow(storage_path=tmp_path, engine="direct")

    with pytest.raises(ValueError, match="only resource_lifetime='execution'"):
        workflow.create_engine(resource_lifetime="engine")
    with pytest.raises(ValueError, match="Wetlands"):
        workflow.create_engine(env_manager=object())


def test_parsl_factory_rejects_wetlands_manager_and_missing_runtime(
    tmp_path,
) -> None:
    workflow = Workflow(storage_path=tmp_path, engine="parsl")

    with pytest.raises(ValueError, match="env_manager"):
        workflow.create_engine(
            parsl_config=object(),
            executor_bindings={"cpu": _binding()},
            env_manager=object(),
        )
    with pytest.raises(ValueError, match="executor_bindings"):
        workflow.create_engine(parsl_config=object())
    with pytest.raises(ValueError, match="exactly one"):
        workflow.create_engine(executor_bindings={"cpu": _binding()})
    with pytest.raises(ValueError, match="exactly one"):
        workflow.create_engine(
            parsl_config=object(),
            dfk=object(),
            executor_bindings={"cpu": _binding()},
        )


def test_bare_parsl_compute_has_actionable_attached_engine_error(
    tmp_path,
) -> None:
    workflow = Workflow(engine="parsl", storage_path=tmp_path)
    with workflow:
        node = CountingTable()(value=3)

    with pytest.raises(ValueError) as exc_info:
        workflow.compute(node)

    message = str(exc_info.value)
    assert "ParslEngine(parsl_config=config" in message
    assert "engine=engine" in message
    assert not (tmp_path / "views").exists()


def test_explicit_parsl_engine_overrides_stored_workflow_backend(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dfk = object()
    monkeypatch.setattr(
        engine_module,
        "require_parsl",
        lambda: SimpleNamespace(DataFlowKernel=None),
    )

    class ResultEngine(ParslEngine):
        def _execute_attached(
            self,
            targets: Any,
            executed_workflow: Any,
            attached_dfk: Any,
        ) -> Any:
            del executed_workflow
            assert attached_dfk is dfk
            return {
                target.name: pd.DataFrame({"value": [9]}, index=["row"])
                for target in targets
            }

    engine = ResultEngine(
        dfk=dfk,
        executor_bindings={"cpu": _binding()},
        resource_lifetime="external",
        execution="sequential",
    )
    workflow = Workflow(
        engine="direct",
        execution="parallel",
        storage_path=tmp_path,
    )
    with workflow:
        node = CountingTable()(value=3)

    result = workflow.compute(node, engine=engine)

    assert result.loc["row", "value"] == 9


def test_shared_parsl_engine_overlap_fails_before_second_run_view(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered = Event()
    release = Event()
    failures: list[BaseException] = []
    monkeypatch.setattr(
        engine_module,
        "require_parsl",
        lambda: SimpleNamespace(DataFlowKernel=None),
    )

    class BlockingEngine(ParslEngine):
        def _execute_attached(
            self,
            targets: Any,
            executed_workflow: Any,
            attached_dfk: Any,
        ) -> Any:
            del executed_workflow, attached_dfk
            entered.set()
            release.wait(timeout=5)
            return {
                target.name: pd.DataFrame({"value": [1]}, index=["row"])
                for target in targets
            }

    engine = BlockingEngine(
        dfk=object(),
        executor_bindings={"cpu": _binding()},
        resource_lifetime="external",
    )
    first = Workflow(engine="direct", storage_path=tmp_path / "first")
    second = Workflow(engine="direct", storage_path=tmp_path / "second")
    with first:
        first_node = CountingTable()(value=1)
    with second:
        second_node = CountingTable()(value=2)

    def execute_first() -> None:
        try:
            first.compute(first_node, engine=engine)
        except BaseException as exc:
            failures.append(exc)

    thread = Thread(target=execute_first)
    thread.start()
    assert entered.wait(timeout=5)

    with pytest.raises(RuntimeError, match="active execution"):
        second.compute(second_node, engine=engine)

    assert not (tmp_path / "second" / "views").exists()
    release.set()
    thread.join(timeout=5)
    assert failures == []


def test_workflow_steps_reserve_explicit_parsl_engine_eagerly(
    tmp_path,
) -> None:
    engine = _NoDispatchEngine(
        dfk=object(),
        executor_bindings={"cpu": _binding()},
        resource_lifetime="external",
    )
    first = Workflow(engine="direct", storage_path=tmp_path / "first")
    second = Workflow(engine="direct", storage_path=tmp_path / "second")
    with first:
        first_node = CountingTable()(value=1)
    with second:
        second_node = CountingTable()(value=2)

    steps = first.compute_steps(first_node, engine=engine)
    with pytest.raises(RuntimeError, match="active execution"):
        second.compute(second_node, engine=engine)
    assert not (tmp_path / "first" / "views").exists()
    assert not (tmp_path / "second" / "views").exists()

    steps.close()


class _NoDispatchEngine(ParslEngine):
    def _execute_attached(
        self,
        targets: Any,
        workflow: Any,
        dfk: Any,
    ) -> Any:
        del targets, workflow, dfk
        raise AssertionError("dispatch is not expected")
