"""Fixtures shared by deterministic integration tests."""

from pathlib import Path

import pytest


@pytest.fixture
def tmp_workspace(tmp_path: Path) -> Path:
    """Create a temporary workspace with sample image files."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    for name in ["cell_01.tif", "cell_02.tif", "cell_03.tif"]:
        (data_dir / name).write_text(f"FAKE_IMAGE_{name}")
    return tmp_path


@pytest.fixture
def tmp_workspace_with_metadata(tmp_path: Path) -> Path:
    """Create a workspace with files named for metadata extraction."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    for patient, image_slice in [
        ("patientA", "001"),
        ("patientA", "002"),
        ("patientB", "001"),
    ]:
        (data_dir / f"{patient}_{image_slice}.tif").write_text("FAKE")
    return tmp_path


@pytest.fixture
def tmp_workspace_two_sources(tmp_path: Path) -> Path:
    """Create two separate data directories for multi-source tests."""
    mri_dir = tmp_path / "mri"
    ct_dir = tmp_path / "ct"
    mri_dir.mkdir()
    ct_dir.mkdir()
    for patient_id in ["P001", "P002", "P003"]:
        (mri_dir / f"{patient_id}_mri.nii").write_text("MRI_DATA")
        (ct_dir / f"{patient_id}_ct.nii").write_text("CT_DATA")

    csv_path = tmp_path / "patients.csv"
    csv_path.write_text("patient_id,age,sex\nP001,65,M\nP002,42,F\nP003,71,M\n")
    return tmp_path


@pytest.fixture
def tmp_workspace_with_quality(tmp_path: Path) -> Path:
    """Create a workspace with a CSV containing quality scores."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    for index in range(5):
        (data_dir / f"img_{index:03d}.tif").write_text("FAKE")

    csv_path = tmp_path / "quality.csv"
    csv_path.write_text(
        "filename,quality\n"
        "img_000.tif,0.9\n"
        "img_001.tif,0.3\n"
        "img_002.tif,0.8\n"
        "img_003.tif,0.2\n"
        "img_004.tif,0.7\n"
    )
    return tmp_path
