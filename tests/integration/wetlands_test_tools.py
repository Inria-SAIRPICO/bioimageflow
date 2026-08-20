"""Standalone tool definitions for Wetlands integration tests.

These tools run inside Wetlands worker processes and must only depend on
bioimageflow_core (auto-injected by the environment manager). They are loaded
through the canonical strict worker origin and processing-task protocol.
"""

import time
import sys
from pathlib import Path
from typing import Annotated, Any

from bioimageflow_core import (
    Arguments,
    EnvironmentSpec,
    IOModel,
    ImageSpec,
    ProcessingTool,
    RowConsumption,
    ResourceSpec,
    Semantic,
    Template,
)


# ── Shared environment (no heavy deps needed for stubs) ───────────
# Include local bioimageflow-core so the worker gets the dev version
# (with task parameter support) instead of the PyPI release.
_CORE_PKG = str(Path(__file__).resolve().parents[2] / "packages" / "bioimageflow-core")

stub_env = EnvironmentSpec(
    name="stub_test_env",
    dependencies={"pip": [f"bioimageflow-core @ file://{_CORE_PKG}"], "python": "3.13"},
)

gpu_env = EnvironmentSpec(
    name="gpu_test_env",
    dependencies={"pip": [f"bioimageflow-core @ file://{_CORE_PKG}"], "python": "3.13"},
)


# ── Feature 1: Row parallelism ───────────────────────────────────

class SimpleRowTool(ProcessingTool):
    """Minimal row tool — writes a file per row."""
    row_consumption = RowConsumption.MAPPED
    display_name = "Simple Row Tool"
    environment = stub_env

    class Inputs(IOModel):
        input_path: Annotated[Path, ImageSpec(semantics={Semantic.INTENSITY})]

    class Outputs(IOModel):
        output_path: Path = Template("{input_path.stem}_out_{row_index}.txt")
        value: float

    def process_row(self, arguments: Arguments, *, context: object | None = None) -> Any:
        out = Path(arguments.output_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(f"processed:{arguments.input_path}")
        return self.Outputs(output_path=out, value=42.0)


class SlowRowTool(ProcessingTool):
    """Row tool with a small delay — for testing parallelism timing."""
    row_consumption = RowConsumption.MAPPED
    display_name = "Slow Row Tool"
    environment = stub_env

    class Inputs(IOModel):
        input_path: Annotated[Path, ImageSpec(semantics={Semantic.INTENSITY})]

    class Outputs(IOModel):
        output_path: Path = Template("{input_path.stem}_slow_{row_index}.txt")
        elapsed: float

    def process_row(self, arguments: Arguments, *, context: object | None = None) -> Any:
        t0 = time.monotonic()
        time.sleep(0.3)
        out = Path(arguments.output_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("slow")
        return self.Outputs(output_path=out, elapsed=time.monotonic() - t0)


class ErrorRowTool(ProcessingTool):
    """Always raises — for testing error propagation."""
    row_consumption = RowConsumption.MAPPED
    display_name = "Error Row Tool"
    environment = stub_env

    class Inputs(IOModel):
        input_path: Annotated[Path, ImageSpec(semantics={Semantic.INTENSITY})]

    class Outputs(IOModel):
        result: float

    def process_row(self, arguments: Arguments, *, context: object | None = None) -> Any:
        raise RuntimeError("Intentional test error")


class WorkerStreamTool(ProcessingTool):
    """Writes to worker stdout and stderr for console routing tests."""
    row_consumption = RowConsumption.MAPPED
    display_name = "Worker Stream Tool"
    environment = stub_env

    class Inputs(IOModel):
        input_path: Annotated[Path, ImageSpec(semantics={Semantic.INTENSITY})]

    class Outputs(IOModel):
        output_path: Path = Template("{input_path.stem}_stream_{row_index}.txt")

    def process_row(self, arguments: Arguments, *, context: object | None = None) -> Any:
        print("worker routine stdout")
        print("worker actual stderr", file=sys.stderr)
        out = Path(arguments.output_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("streamed")
        return self.Outputs(output_path=out)


# ── Feature 2: GPU-aware worker assignment ────────────────────────

class GpuTool(ProcessingTool):
    """Declares a portable GPU worker requirement."""
    row_consumption = RowConsumption.MAPPED
    display_name = "GPU Tool"
    environment = gpu_env
    resources = ResourceSpec(gpu=1)

    class Inputs(IOModel):
        input_path: Annotated[Path, ImageSpec(semantics={Semantic.INTENSITY})]

    class Outputs(IOModel):
        output_path: Path = Template("{input_path.stem}_gpu_{row_index}.txt")
        value: float

    def process_row(self, arguments: Arguments, *, context: object | None = None) -> Any:
        out = Path(arguments.output_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("gpu_processed")
        return self.Outputs(output_path=out, value=1.0)


# ── Feature 3: Sub-row progress reporting ─────────────────────────

class ProgressReportingTool(ProcessingTool):
    """Reports sub-row progress via task.update()."""
    row_consumption = RowConsumption.MAPPED
    display_name = "Progress Tool"
    environment = stub_env

    class Inputs(IOModel):
        input_path: Annotated[Path, ImageSpec(semantics={Semantic.INTENSITY})]

    class Outputs(IOModel):
        output_path: Path = Template("{input_path.stem}_prog_{row_index}.txt")

    def process_row(self, arguments: Arguments, *, context: object | None = None, task=None) -> Any:
        steps = 5
        for i in range(steps):
            if task is not None:
                task.update(message=f"Step {i+1}/{steps}", current=i + 1, maximum=steps)
            time.sleep(0.05)
        out = Path(arguments.output_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("done")
        return self.Outputs(output_path=out)


# ── Feature 3: Batch with progress ───────────────────────────────

class BatchTool(ProcessingTool):
    """Batch processor — tests submit() path."""
    row_consumption = RowConsumption.MAPPED
    display_name = "Batch Tool"
    environment = stub_env

    class Inputs(IOModel):
        input_path: Annotated[Path, ImageSpec(semantics={Semantic.INTENSITY})]

    class Outputs(IOModel):
        output_path: Path = Template("{input_path.stem}_batch_{row_index}.txt")
        value: float

    def process_batch(self, arguments_list: list[Any], *, context: object | None = None, task=None) -> Any:
        results = []
        for i, args in enumerate(arguments_list):
            if task is not None:
                task.update(
                    message=f"Batch item {i+1}/{len(arguments_list)}",
                    current=i + 1,
                    maximum=len(arguments_list),
                )
            out = Path(args.output_path)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(f"batch:{args.input_path}")
            results.append(self.Outputs(output_path=out, value=float(i)))
        return results


# ── Feature 4: Cancellation ──────────────────────────────────────

class CancellableRowTool(ProcessingTool):
    """Slow tool for testing cancellation — sleeps long enough to be cancelled."""
    row_consumption = RowConsumption.MAPPED
    display_name = "Cancellable Tool"
    environment = stub_env

    class Inputs(IOModel):
        input_path: Annotated[Path, ImageSpec(semantics={Semantic.INTENSITY})]

    class Outputs(IOModel):
        output_path: Path = Template("{input_path.stem}_cancel_{row_index}.txt")

    def process_row(self, arguments: Arguments, *, context: object | None = None) -> Any:
        # Sleep long enough for cancellation to arrive
        time.sleep(5)
        out = Path(arguments.output_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("should_not_complete")
        return self.Outputs(output_path=out)


# ── Feature 5: Branch parallelism ────────────────────────────────
# Uses SimpleRowTool and SlowRowTool above — no extra tool needed.
