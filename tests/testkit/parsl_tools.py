"""Small importable processing tools for real Parsl executor tests."""

from __future__ import annotations

import time
import os

from bioimageflow_core import Arguments, EnvironmentSpec, IOModel, ProcessingTool


PARSL_TEST_ENV = EnvironmentSpec(
    name="parsl-test",
    dependencies={"python": "3.10", "pip": ["bioimageflow-core==0.1.7"]},
)


class ParslIncrement(ProcessingTool):
    """Return one incremented scalar from a source processing invocation."""

    environment = PARSL_TEST_ENV

    class Inputs(IOModel):
        value: int = 1

    class Outputs(IOModel):
        value: int

    def process_row(self, arguments: Arguments) -> "ParslIncrement.Outputs":
        return self.Outputs(value=arguments.value + 1)


class ParslExplode(ProcessingTool):
    """Return a deterministic ordered one-to-many result."""

    environment = PARSL_TEST_ENV

    class Inputs(IOModel):
        value: int

    class Outputs(IOModel):
        value: int

    def process_row(
        self,
        arguments: Arguments,
    ) -> list["ParslExplode.Outputs"]:
        return [
            self.Outputs(value=arguments.value),
            self.Outputs(value=arguments.value + 1),
        ]


class ParslDelayed(ProcessingTool):
    """Delay smaller input values less so task completion is out of order."""

    environment = PARSL_TEST_ENV

    class Inputs(IOModel):
        value: int

    class Outputs(IOModel):
        value: int

    def process_row(self, arguments: Arguments) -> "ParslDelayed.Outputs":
        time.sleep(arguments.value * 0.02)
        return self.Outputs(value=arguments.value * 10)


class ParslBatch(ProcessingTool):
    """Exercise one whole-node batch with nested output groups."""

    environment = PARSL_TEST_ENV

    class Inputs(IOModel):
        value: int

    class Outputs(IOModel):
        value: int

    def process_batch(
        self,
        arguments_list: list[Arguments],
    ) -> list[list["ParslBatch.Outputs"]]:
        return [
            [
                self.Outputs(value=arguments.value),
                self.Outputs(value=arguments.value + 100),
            ]
            for arguments in arguments_list
        ]


class ParslFail(ProcessingTool):
    """Raise a deterministic worker error for correlation tests."""

    environment = PARSL_TEST_ENV

    class Inputs(IOModel):
        value: int = 1

    class Outputs(IOModel):
        value: int

    def process_row(self, arguments: Arguments) -> "ParslFail.Outputs":
        raise RuntimeError(f"remote failure {arguments.value}")


class ParslProcessIdentity(ProcessingTool):
    """Expose process and persistent worker-instance identity."""

    environment = PARSL_TEST_ENV

    class Inputs(IOModel):
        value: int

    class Outputs(IOModel):
        value: int
        process_id: int
        instance_id: int

    def process_row(
        self,
        arguments: Arguments,
    ) -> "ParslProcessIdentity.Outputs":
        return self.Outputs(
            value=arguments.value,
            process_id=os.getpid(),
            instance_id=id(self),
        )


class ParslEmptyBatch(ProcessingTool):
    """Run once for an empty aligned input using the synthetic anchor."""

    environment = PARSL_TEST_ENV
    run_empty_batch = True

    class Inputs(IOModel):
        value: int

    class Outputs(IOModel):
        count: int

    def process_batch(
        self,
        arguments_list: list[Arguments],
    ) -> list[list["ParslEmptyBatch.Outputs"]]:
        if len(arguments_list) != 1:
            raise ValueError("empty batch must receive one synthetic row")
        return [[self.Outputs(count=0)]]
