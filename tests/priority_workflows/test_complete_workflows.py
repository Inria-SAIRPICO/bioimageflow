from __future__ import annotations

import os
from pathlib import Path

import imageio.v3 as iio
import numpy as np
import pytest

from tests.priority_workflows.test_workflows import _load_module


pytestmark = [pytest.mark.complete, pytest.mark.wetlands]


def _example(name: str) -> Path:
    root = Path(__file__).resolve().parents[2]
    return root / "example-workflows" / name / "workflow.py"


def _write_multichannel_input(data_dir: Path) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    image = np.zeros((3, 32, 32), dtype=np.float32)
    image[2, 8:15, 8:15] = 300
    image[2, 19:26, 18:25] = 500
    image[0, 10, 10] = 200
    image[1, 22, 21] = 250
    iio.imwrite(data_dir / "synthetic_cyx.tif", image, photometric="minisblack")


@pytest.mark.model_runtime
def test_cellpose_stardist_workflow_executes_with_real_model_runtimes(
    tmp_path: Path,
    complete_wetlands_config: dict,
) -> None:
    data_dir = tmp_path / "cellpose_stardist" / "data"
    _write_multichannel_input(data_dir)

    module = _load_module(_example("cellpose3_stardist"))
    wf, cellpose, stardist = module.build_segmentation_workflow(
        data_dir=str(data_dir),
        storage_path=str(tmp_path / "cellpose_stardist" / "bif"),
        use_wetlands=True,
        wetlands_config=complete_wetlands_config,
    )

    result = wf.compute(cellpose, stardist)

    cellpose_result = result["cellpose3_nuclei"]
    stardist_result = result["stardist_nuclei"]
    assert Path(cellpose_result.iloc[0]["mask"]).exists()
    assert Path(stardist_result.iloc[0]["mask"]).exists()
    assert int(cellpose_result.iloc[0]["cell_count"]) >= 0
    assert int(stardist_result.iloc[0]["object_count"]) >= 0


@pytest.mark.external_binary
def test_parameter_space_workflow_executes_with_real_atlas_binary(
    tmp_path: Path,
    complete_wetlands_config: dict,
) -> None:
    data_dir = tmp_path / "atlas_parameter_sweep" / "data"
    data_dir.mkdir(parents=True)
    image = np.zeros((24, 24), dtype=np.uint16)
    image[8, 9] = 4000
    image[17, 16] = 4500
    iio.imwrite(data_dir / "spots.tif", image)

    module = _load_module(_example("parameter_space_exploration"))
    wf, terminal = module.build_parameter_space_workflow(
        data_dir=str(data_dir),
        storage_path=str(tmp_path / "atlas_parameter_sweep" / "bif"),
        use_wetlands=True,
        wetlands_config=complete_wetlands_config,
    )

    result = wf.compute(terminal)

    assert len(result) == 6
    assert int(result.iloc[0]["image_count"]) == 6
    assert Path(result.iloc[0]["mosaic_path"]).exists()


@pytest.mark.external_binary
@pytest.mark.sairpico_binary
def test_sairpico_workflow_executes_with_real_binaries(
    tmp_path: Path,
    complete_wetlands_config: dict,
) -> None:
    module = _load_module(_example("sairpico_restoration_smoke"))
    wf, terminal = module.build_workflow(
        storage_path=str(tmp_path / "sairpico"),
        use_wetlands=True,
        wetlands_config=complete_wetlands_config,
    )

    result = wf.compute(terminal)

    assert len(result) == 1
    assert Path(result.iloc[0]["output_image"]).exists()
    assert Path(result.iloc[0]["output_image_1"]).exists()


@pytest.mark.public_data
@pytest.mark.external_binary
@pytest.mark.model_runtime
def test_fish_public_cil_workflow_executes_when_downloads_are_allowed(
    tmp_path: Path,
    complete_wetlands_config: dict,
) -> None:
    if os.environ.get("BIOIMAGEFLOW_ALLOW_PUBLIC_DOWNLOADS") != "1":
        pytest.skip("set BIOIMAGEFLOW_ALLOW_PUBLIC_DOWNLOADS=1 to download CIL data")

    module = _load_module(_example("fish_analysis"))
    wf, terminal = module.build_fish_workflow(
        storage_path=str(tmp_path / "fish_public" / "bif"),
        data_dir=str(tmp_path / "fish_public" / "data"),
        use_wetlands=True,
        wetlands_config=complete_wetlands_config,
    )

    result = wf.compute(terminal)

    assert not result.empty
    assert {"avg_fols2_per_nucleus", "avg_csfr1_per_nucleus"} <= set(result.columns)


@pytest.mark.slow
@pytest.mark.parametrize(
    ("workflow_name", "artifact_column"),
    [
        ("puncta_analysis", None),
        ("restoration_benchmark", "restored_image"),
        ("tracking_analysis", None),
    ],
)
def test_specialized_workflow_acceptance_smoke(
    tmp_path: Path,
    workflow_name: str,
    artifact_column: str,
    complete_wetlands_config: dict,
) -> None:
    module = _load_module(_example(workflow_name))
    wf, terminal = module.build_workflow(
        storage_path=str(tmp_path / workflow_name),
        use_wetlands=True,
        wetlands_config=complete_wetlands_config,
    )

    result = wf.compute(terminal)

    assert not result.empty
    if artifact_column is not None:
        artifact = Path(result.iloc[0][artifact_column])
        assert artifact.exists()
