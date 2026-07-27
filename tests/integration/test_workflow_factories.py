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
def test_workflow_definition_factory_contract(
    relative_path: str,
    tmp_path: Path,
) -> None:
    path = Path(relative_path)
    first = Workflow.from_python(path, storage_path=tmp_path / "first")
    second = Workflow.from_python(path, storage_path=tmp_path / "second")

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


@pytest.mark.parametrize(
    ("relative_path", "expected_display_name", "expected_inputs"),
    (
        (
            "example_workflows/fish_analysis/workflow.py",
            "Fish Analysis",
            [],
        ),
        (
            "example_workflows/parameter_space_exploration/workflow.py",
            "Parameters Space Exploration",
            ["marker_channel"],
        ),
    ),
)
def test_platform_demo_factories_are_self_contained(
    relative_path: str,
    expected_display_name: str,
    expected_inputs: list[str],
    tmp_path: Path,
) -> None:
    exported = Workflow.from_python(
        Path(relative_path),
        storage_path=tmp_path,
    ).to_dict(
        include_custom_tools=True
    )
    graph = exported["workflow"]

    assert graph["display_name"] == expected_display_name
    assert [item["name"] for item in graph["interface"]["inputs"]] == expected_inputs
    assert any(node["tool_class"] == "DownloadImages" for node in graph["nodes"])
    assert "data_dir" not in str(graph)
    assert "output_dir" not in str(graph)
