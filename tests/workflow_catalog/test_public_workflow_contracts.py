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


def _workflow_page(workflow_name: str) -> Path:
    return _root() / "docs/source/workflows" / f"{workflow_name}.rst"


def _extract_image_directives(text: str) -> list[tuple[str, str]]:
    directives: list[tuple[str, str]] = []
    lines = text.splitlines()
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped.startswith((".. image:: ", ".. figure:: ")):
            continue
        image_path = stripped.split("::", 1)[1].strip()
        alt = ""
        for option_line in lines[index + 1 : index + 8]:
            if option_line.strip().startswith(":alt:"):
                alt = option_line.split(":alt:", 1)[1].strip()
                break
            if option_line and not option_line.startswith(" "):
                break
        directives.append((image_path, alt))
    return directives


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

    assert ".. toctree::" in catalog
    assert "Goal\n" not in catalog
    assert "How it works\n" not in catalog
    assert "Interpretation\n" not in catalog
    assert len(catalog.splitlines()) < 40
    for workflow_name in PUBLIC_WORKFLOWS:
        assert workflow_name in catalog
        assert f"   {workflow_name}" in catalog
    for workflow_name in RETIRED_PUBLIC_WORKFLOWS:
        assert workflow_name not in catalog


@pytest.mark.parametrize("workflow_name", sorted(PUBLIC_WORKFLOWS))
def test_public_workflow_has_tutorial_page_with_diagram_and_images(workflow_name: str) -> None:
    page = _workflow_page(workflow_name)
    assert page.exists()
    text = page.read_text()

    assert workflow_name in text
    assert f"example-workflows/{workflow_name}/workflow.py" in text
    assert '.. raw:: html\n\n   <pre class="mermaid">' in text
    assert "flowchart" in text
    assert "Analysis " + "question" not in text
    for heading in ["Goal\n", "Data\n", "Command\n", "How it works\n", "Results\n", "Interpretation\n"]:
        assert heading not in text
    for contract_term in ["normal_ci", "terminal_node", "required_columns", "deterministic_acceptance"]:
        assert contract_term not in text
    for ci_term in [
        "development checks",
        "default checks",
        "automated checks",
        "automated validation",
        "Tests can",
        "mock the",
        "fake backend",
        "smoke test",
    ]:
        assert ci_term not in text

    image_directives = _extract_image_directives(text)
    assert len(image_directives) >= 2
    for relative_image, alt in image_directives:
        image_path = page.parent / relative_image
        assert image_path.exists(), relative_image
        assert image_path.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp", ".svg"}
        assert image_path.parent.name == workflow_name
        assert alt
