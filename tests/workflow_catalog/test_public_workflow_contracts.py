from __future__ import annotations

from pathlib import Path

import pytest
import yaml


PUBLIC_WORKFLOWS = {
    "fish_analysis",
    "parameter_space_exploration",
    "bbbc038_segmentation_benchmark",
    "cell_counting_phenotyping",
    "low_snr_restoration",
    "sairpico_deconvolution",
    "live_cell_tracking",
}

RETIRED_PUBLIC_WORKFLOWS = {
    "fish_analysis" + "_sub_workflows",
    "ome" + "_normalization",
    "puncta" + "_analysis",
    "restoration" + "_benchmark",
    "sairpico_restoration" + "_smoke",
    "tracking" + "_analysis",
    "cellpose3" + "_stardist",
}


def _root() -> Path:
    return Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("workflow_name", sorted(PUBLIC_WORKFLOWS))
def test_public_workflows_have_data_and_output_contracts(workflow_name: str) -> None:
    workflow_dir = _root() / "example-workflows" / workflow_name
    manifest_path = workflow_dir / "data_manifest.yml"
    expected_path = workflow_dir / "expected_outputs.yml"

    assert (workflow_dir / "workflow.py").exists()
    assert manifest_path.exists()
    assert expected_path.exists()

    manifest = yaml.safe_load(manifest_path.read_text())
    expected = yaml.safe_load(expected_path.read_text())

    assert manifest["workflow"] == workflow_name
    assert expected["workflow"] == workflow_name
    assert manifest["data"]["normal_ci"]
    assert "public_data" in manifest["data"]
    assert expected["outputs"]["terminal_node"]
    assert expected["outputs"]["required_columns"]


def test_public_workflow_catalog_lists_only_planned_workflows() -> None:
    catalog = (_root() / "docs/source/workflows/index.rst").read_text()

    for workflow_name in PUBLIC_WORKFLOWS:
        assert workflow_name in catalog
    for workflow_name in RETIRED_PUBLIC_WORKFLOWS:
        assert workflow_name not in catalog
