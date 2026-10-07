"""Ordinary single-module source for attached array-export regressions."""

from bioimageflow_core import EnvironmentSpec, IOModel, ProcessingTool, RowConsumption
from bioimageflow_core.types import SharedArray


class ArrayInputs(IOModel):
    value: int = 11


class ArrayOutputs(IOModel):
    image: SharedArray


class ExportArray(ProcessingTool):
    row_consumption = RowConsumption.MAPPED
    environment = EnvironmentSpec(name="bundle-export-direct", dependencies={})
    Inputs = ArrayInputs
    Outputs = ArrayOutputs

    def process_row(self, arguments, *, context=None):
        import numpy as np
        from bioimageflow_core.shm import create_shared_output

        with create_shared_output(np.full((2, 3), arguments.value, dtype=np.uint16)) as reference:
            return self.Outputs(image=reference)
