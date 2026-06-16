"""Synthetic blur/noise restoration benchmark workflow."""

from pathlib import Path

from bioimageflow import Workflow
from bioimageflow.node import Node
from bioimageflow_restoration_tools import BenchmarkRestoration


def build_workflow(
    storage_path: str = "./restoration_benchmark_results",
    engine: str = "direct",
    wetlands_config: dict | None = None,
) -> tuple[Workflow, Node]:
    """Build the restoration benchmark workflow."""
    storage = Path(storage_path)
    wf = Workflow(
        storage_path=str(storage / "bif"),
        engine=engine,
        wetlands_config=wetlands_config,
    )
    with wf:
        benchmark = BenchmarkRestoration()(
            image_size=64,
            noise_sigma=0.12,
            blur_sigma=1.0,
            seed=13,
            name="synthetic_restoration_benchmark",
        )
    return wf, benchmark


if __name__ == "__main__":
    workflow, terminal = build_workflow()
    print(workflow.compute(terminal).to_string(index=False))
