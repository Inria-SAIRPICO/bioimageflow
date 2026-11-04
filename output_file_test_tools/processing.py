"""Core-only real-worker output publication control."""

import os
from pathlib import Path

from bioimageflow_core import EnvironmentSpec, IOModel, ProcessingTool, RowConsumption, Template


class WorkerNumberFile(ProcessingTool):
    row_consumption = RowConsumption.MAPPED
    environment = EnvironmentSpec(name="output-file-worker-proof", dependencies={"python": "3.12"})

    class Inputs(IOModel):
        image: Path
        threshold: int

    class Outputs(IOModel):
        report: Path = Template("result.txt")
        worker_pid: int

    def process_row(self, arguments):
        report = Path(arguments.report)
        report.write_text(str(arguments.threshold * 2))
        return self.Outputs(report=report, worker_pid=os.getpid())
