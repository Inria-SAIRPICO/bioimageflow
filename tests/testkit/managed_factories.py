"""Importable trusted factories used by managed-validation tests."""

from __future__ import annotations


def _executor(runtime, *, worker_init=None, label="cpu"):
    from parsl.executors import HighThroughputExecutor
    from parsl.providers import SlurmProvider

    return HighThroughputExecutor(
        label=label,
        provider=SlurmProvider(
            init_blocks=0,
            worker_init=runtime.worker_init if worker_init is None else worker_init,
        ),
    )


def valid(runtime):
    from parsl import Config

    from bioimageflow.parsl.factory import ParslFactoryResult, WorkerSlot

    return ParslFactoryResult(
        config=Config(executors=[_executor(runtime)], retries=0),
        executor_bindings={
            "cpu": runtime.executor_binding(slot=WorkerSlot(cpu=2, memory="4 GB"))
        },
    )


def retries(runtime):
    result = valid(runtime)
    result.config.retries = 2
    return result


def label_mismatch(runtime):
    from parsl import Config

    from bioimageflow.parsl.factory import ParslFactoryResult, WorkerSlot

    return ParslFactoryResult(
        config=Config(executors=[_executor(runtime)], retries=0),
        executor_bindings={
            "other": runtime.executor_binding(slot=WorkerSlot(cpu=1))
        },
    )


def wrong_worker_init(runtime):
    from parsl import Config

    from bioimageflow.parsl.factory import ParslFactoryResult, WorkerSlot

    return ParslFactoryResult(
        config=Config(
            executors=[_executor(runtime, worker_init="source something-else")],
            retries=0,
        ),
        executor_bindings={
            "cpu": runtime.executor_binding(slot=WorkerSlot(cpu=1))
        },
    )


def secret_failure(runtime, *, credential):
    del runtime
    print(f"credential={credential}")
    raise RuntimeError(f"could not use {credential}")


def forbidden_dfk(runtime):
    del runtime
    import parsl

    parsl.load()


def slow(runtime):
    del runtime
    import time

    time.sleep(30)


class DerivedResult:
    pass


def wrong_result(runtime):
    del runtime
    return DerivedResult()
