"""Attached Parsl engine resource ownership and execution lifecycle."""

from __future__ import annotations

from concurrent.futures import CancelledError, Future
from threading import Event, Thread
from types import SimpleNamespace
from typing import Any

import pytest

from bioimageflow import (
    ExecutorBinding,
    ExecutorCapabilities,
    ParslEngine,
    WorkerEnvironmentAttestation,
    WorkerSlotCapacity,
)
from bioimageflow.parsl import engine as engine_module


def _binding() -> ExecutorBinding:
    return ExecutorBinding(
        label="cpu",
        environments=(
            WorkerEnvironmentAttestation(
                name="analysis",
                dependency_hash="a" * 64,
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


class _FakeDFK:
    instances: list["_FakeDFK"] = []

    def __init__(self, config: Any) -> None:
        self.config = config
        self.cleanup_calls = 0
        self.instances.append(self)

    def cleanup(self) -> None:
        self.cleanup_calls += 1


class _Harness(ParslEngine):
    def _execute_attached(
        self,
        targets: Any,
        workflow: Any,
        dfk: Any,
    ) -> Any:
        del workflow
        return targets, dfk

    def _execute_steps_attached(
        self,
        targets: Any,
        workflow: Any,
        dfk: Any,
    ):
        del workflow
        yield targets, dfk


@pytest.fixture(autouse=True)
def _fake_parsl(monkeypatch: pytest.MonkeyPatch):
    _FakeDFK.instances.clear()
    module = SimpleNamespace(DataFlowKernel=_FakeDFK)
    monkeypatch.setattr(engine_module, "require_parsl", lambda: module)
    return module


def test_execution_lifetime_creates_and_cleans_one_dfk_per_execution() -> None:
    config = object()
    engine = _Harness(
        parsl_config=config,
        executor_bindings={"cpu": _binding()},
    )

    first = engine.execute(["first"], object())
    second = engine.execute(["second"], object())

    assert first == (["first"], _FakeDFK.instances[0])
    assert second == (["second"], _FakeDFK.instances[1])
    assert [dfk.config for dfk in _FakeDFK.instances] == [config, config]
    assert [dfk.cleanup_calls for dfk in _FakeDFK.instances] == [1, 1]
    assert engine.dfk is None


def test_engine_lifetime_reuses_owned_dfk_until_idempotent_close() -> None:
    engine = _Harness(
        parsl_config=object(),
        executor_bindings={"cpu": _binding()},
        resource_lifetime="engine",
    )

    first = engine.execute(["first"], object())
    second = engine.execute(["second"], object())

    assert first[1] is second[1]
    assert len(_FakeDFK.instances) == 1
    assert engine.dfk is _FakeDFK.instances[0]
    assert _FakeDFK.instances[0].cleanup_calls == 0

    engine.close()
    engine.close()

    assert _FakeDFK.instances[0].cleanup_calls == 1
    assert engine.dfk is None
    with pytest.raises(RuntimeError, match="closed"):
        engine.execute([], object())


def test_close_cleans_retained_dfk_while_execution_is_only_reserved() -> None:
    engine = _Harness(
        parsl_config=object(),
        executor_bindings={"cpu": _binding()},
        resource_lifetime="engine",
    )
    engine.execute([], object())
    retained = _FakeDFK.instances[0]
    engine._reserve_execution()

    engine.close()

    assert retained.cleanup_calls == 1
    assert engine.dfk is None


def test_external_lifetime_never_cleans_caller_dfk() -> None:
    dfk = _FakeDFK(config=object())
    engine = _Harness(
        dfk=dfk,
        executor_bindings={"cpu": _binding()},
        resource_lifetime="external",
    )

    result = engine.execute(["target"], object())
    engine.close()

    assert result == (["target"], dfk)
    assert dfk.cleanup_calls == 0
    assert engine.dfk is dfk


def test_engine_acquisition_does_not_use_global_parsl_loader(
    _fake_parsl: SimpleNamespace,
) -> None:
    def unexpected(*args: Any, **kwargs: Any) -> None:
        del args, kwargs
        raise AssertionError("global Parsl state must not be used")

    _fake_parsl.load = unexpected
    _fake_parsl.clear = unexpected
    _fake_parsl.dfk = unexpected
    engine = _Harness(
        parsl_config=object(),
        executor_bindings={"cpu": _binding()},
    )

    engine.execute([], object())

    assert len(_FakeDFK.instances) == 1


def test_overlapping_execute_fails_before_second_dfk_acquisition() -> None:
    entered = Event()
    release = Event()
    failures: list[BaseException] = []

    class BlockingHarness(_Harness):
        def _execute_attached(self, targets: Any, workflow: Any, dfk: Any) -> Any:
            del targets, workflow
            entered.set()
            release.wait(timeout=5)
            return dfk

    engine = BlockingHarness(
        parsl_config=object(),
        executor_bindings={"cpu": _binding()},
        resource_lifetime="engine",
    )

    def execute() -> None:
        try:
            engine.execute([], object())
        except BaseException as exc:
            failures.append(exc)

    thread = Thread(target=execute)
    thread.start()
    assert entered.wait(timeout=5)

    with pytest.raises(RuntimeError, match="active execution"):
        engine.execute([], object())

    assert len(_FakeDFK.instances) == 1
    release.set()
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert failures == []
    engine.close()


def test_steps_reserve_engine_before_first_iteration_and_close_cleanly() -> None:
    engine = _Harness(
        parsl_config=object(),
        executor_bindings={"cpu": _binding()},
    )

    steps = engine.execute_steps(["target"], object())
    with pytest.raises(RuntimeError, match="active execution"):
        engine.execute([], object())

    steps.close()
    assert _FakeDFK.instances == []

    steps = engine.execute_steps(["target"], object())
    _targets, dfk = next(steps)
    steps.close()

    assert dfk.cleanup_calls == 1
    assert engine.dfk is None


def test_close_releases_unconsumed_reservation_across_threads() -> None:
    engine = _Harness(
        parsl_config=object(),
        executor_bindings={"cpu": _binding()},
    )
    engine._reserve_execution()

    thread = Thread(target=engine.close)
    thread.start()
    thread.join(timeout=5)

    assert not thread.is_alive()
    assert _FakeDFK.instances == []
    with pytest.raises(RuntimeError, match="closed"):
        engine.execute([], object())


def test_close_cancels_and_drains_only_registered_external_work() -> None:
    entered = Event()
    submitted: Future[None] = Future()
    unrelated: Future[None] = Future()
    failures: list[BaseException] = []
    dfk = _FakeDFK(config=object())

    class FutureHarness(_Harness):
        def _execute_attached(self, targets: Any, workflow: Any, dfk: Any) -> Any:
            del targets, workflow, dfk
            self._register_future(submitted)
            entered.set()
            return submitted.result()

    engine = FutureHarness(
        dfk=dfk,
        executor_bindings={"cpu": _binding()},
        resource_lifetime="external",
    )

    def execute() -> None:
        try:
            engine.execute([], object())
        except BaseException as exc:
            failures.append(exc)

    thread = Thread(target=execute)
    thread.start()
    assert entered.wait(timeout=5)

    engine.close()
    thread.join(timeout=5)

    assert not thread.is_alive()
    assert submitted.cancelled()
    assert not unrelated.cancelled()
    assert len(failures) == 1
    assert isinstance(failures[0], CancelledError)
    assert dfk.cleanup_calls == 0


def test_two_engines_sharing_external_dfk_own_only_their_futures() -> None:
    dfk = _FakeDFK(config=object())
    first_future: Future[None] = Future()
    second_future: Future[None] = Future()
    first_entered = Event()
    second_entered = Event()
    failures: list[BaseException] = []

    class SharedHarness(_Harness):
        def __init__(self, future: Future[None], entered: Event) -> None:
            super().__init__(
                dfk=dfk,
                executor_bindings={"cpu": _binding()},
                resource_lifetime="external",
            )
            self.future = future
            self.entered = entered

        def _execute_attached(self, targets: Any, workflow: Any, dfk: Any) -> Any:
            del targets, workflow, dfk
            self._register_future(self.future)
            self.entered.set()
            return self.future.result()

    first = SharedHarness(first_future, first_entered)
    second = SharedHarness(second_future, second_entered)

    def execute(engine: ParslEngine) -> None:
        try:
            engine.execute([], object())
        except BaseException as exc:
            failures.append(exc)

    first_thread = Thread(target=execute, args=(first,))
    second_thread = Thread(target=execute, args=(second,))
    first_thread.start()
    second_thread.start()
    assert first_entered.wait(timeout=5)
    assert second_entered.wait(timeout=5)

    first.close()
    assert first_future.cancelled()
    assert not second_future.cancelled()
    second_future.set_result(None)
    first_thread.join(timeout=5)
    second_thread.join(timeout=5)
    second.close()

    assert not first_thread.is_alive()
    assert not second_thread.is_alive()
    assert len(failures) == 1
    assert isinstance(failures[0], CancelledError)
    assert dfk.cleanup_calls == 0


def test_close_cannot_overtake_atomic_submission_registration() -> None:
    submission_entered = Event()
    release_submission = Event()
    submitted: Future[None] = Future()
    failures: list[BaseException] = []

    class SubmitHarness(_Harness):
        def _execute_attached(self, targets: Any, workflow: Any, dfk: Any) -> Any:
            del targets, workflow, dfk

            def submit() -> Future[None]:
                submission_entered.set()
                assert release_submission.wait(timeout=5)
                return submitted

            future = self._submit_future(submit)
            return future.result()

    engine = SubmitHarness(
        parsl_config=object(),
        executor_bindings={"cpu": _binding()},
        resource_lifetime="engine",
    )

    def execute() -> None:
        try:
            engine.execute([], object())
        except BaseException as exc:
            failures.append(exc)

    execution_thread = Thread(target=execute)
    execution_thread.start()
    assert submission_entered.wait(timeout=5)
    close_thread = Thread(target=engine.close)
    close_thread.start()
    assert close_thread.is_alive()

    release_submission.set()
    execution_thread.join(timeout=5)
    close_thread.join(timeout=5)

    assert not execution_thread.is_alive()
    assert not close_thread.is_alive()
    assert submitted.cancelled()
    assert len(failures) == 1
    assert isinstance(failures[0], CancelledError)
    assert _FakeDFK.instances[0].cleanup_calls == 1


def test_close_waits_for_noncancellable_writer_before_owned_cleanup() -> None:
    writer_registered = Event()
    submitted: Future[None] = Future()
    assert submitted.set_running_or_notify_cancel()
    failures: list[BaseException] = []

    class WriterHarness(_Harness):
        def _execute_attached(self, targets: Any, workflow: Any, dfk: Any) -> Any:
            del targets, workflow, dfk
            self._register_future(submitted)
            writer_registered.set()
            return submitted.result()

    engine = WriterHarness(
        parsl_config=object(),
        executor_bindings={"cpu": _binding()},
        resource_lifetime="engine",
    )

    def execute() -> None:
        try:
            engine.execute([], object())
        except BaseException as exc:
            failures.append(exc)

    execution_thread = Thread(target=execute)
    execution_thread.start()
    assert writer_registered.wait(timeout=5)
    dfk = _FakeDFK.instances[0]
    close_thread = Thread(target=engine.close)
    close_thread.start()

    assert close_thread.is_alive()
    assert dfk.cleanup_calls == 0
    submitted.set_result(None)
    execution_thread.join(timeout=5)
    close_thread.join(timeout=5)

    assert not execution_thread.is_alive()
    assert not close_thread.is_alive()
    assert failures == []
    assert dfk.cleanup_calls == 1


def test_effective_execution_uses_root_policy_only_for_workflow_mode() -> None:
    workflow = SimpleNamespace(execution="sequential")
    inherited = _Harness(
        parsl_config=object(),
        executor_bindings={"cpu": _binding()},
    )
    overridden = _Harness(
        parsl_config=object(),
        executor_bindings={"cpu": _binding()},
        execution="parallel",
    )

    assert inherited.effective_execution(workflow) == "sequential"
    assert overridden.effective_execution(workflow) == "parallel"
