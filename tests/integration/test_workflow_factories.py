"""Maintained inventory and contract test for shipped workflow definitions."""

from pathlib import Path

import pytest

from bioimageflow import Workflow


WORKFLOW_DEFINITION_MODULES = (
    "example_workflows/bbbc038_segmentation_benchmark/workflow.py",
    "example_workflows/cell_counting_phenotyping/workflow.py",
    "example_workflows/fish_analysis/workflow.py",
    "example_workflows/fish_analysis/tools/marker_spot_analysis.py",
    "example_workflows/live_cell_tracking/workflow.py",
    "example_workflows/low_snr_restoration/workflow.py",
    "example_workflows/parameter_space_exploration/workflow.py",
    "example_workflows/sairpico_deconvolution/workflow.py",
)


@pytest.mark.parametrize("relative_path", WORKFLOW_DEFINITION_MODULES)
def test_workflow_definition_factory_contract(relative_path: str) -> None:
    path = Path(relative_path)
    first = Workflow.from_python(path)
    second = Workflow.from_python(path)

    assert isinstance(first, Workflow)
    assert isinstance(second, Workflow)
    assert first is not second
    assert first.validate() == []
    assert second.validate() == []


def test_inventory_covers_every_shipped_example_definition() -> None:
    discovered = {
        path.as_posix()
        for path in Path("example_workflows").rglob("*.py")
        if "def build_workflow(" in path.read_text()
    }
    maintained = set(WORKFLOW_DEFINITION_MODULES)
    assert discovered == maintained
