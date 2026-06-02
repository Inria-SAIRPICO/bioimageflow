from __future__ import annotations

import importlib.util
import os
import shutil
from pathlib import Path

import imageio.v3 as iio
import numpy as np
import pandas as pd
import pytest

from tests.priority_workflows.test_workflows import _load_module


pytestmark = pytest.mark.complete


def _example(name: str) -> Path:
    root = Path(__file__).resolve().parents[2]
    return root / "example-workflows" / name / "workflow.py"


def _require_commands(*commands: str) -> None:
    missing = [command for command in commands if shutil.which(command) is None]
    if missing:
        pytest.skip(f"missing required command(s): {', '.join(missing)}")


def _require_modules(*module_names: str) -> None:
    missing = [
        module_name
        for module_name in module_names
        if importlib.util.find_spec(module_name) is None
    ]
    if missing:
        pytest.skip(f"missing required Python module(s): {', '.join(missing)}")


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
) -> None:
    _require_modules("cellpose", "csbdeep", "stardist")
    data_dir = tmp_path / "cellpose_stardist" / "data"
    _write_multichannel_input(data_dir)

    module = _load_module(_example("cellpose3_stardist"))
    wf, cellpose, stardist = module.build_segmentation_workflow(
        data_dir=str(data_dir),
        storage_path=str(tmp_path / "cellpose_stardist" / "bif"),
    )
    wf.use_wetlands = False

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
) -> None:
    _require_commands("atlas")
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
    )
    wf.use_wetlands = False

    result = wf.compute(terminal)

    assert len(result) == 6
    assert int(result.iloc[0]["image_count"]) == 6
    assert Path(result.iloc[0]["mosaic_path"]).exists()


@pytest.mark.external_binary
@pytest.mark.sairpico_binary
def test_sairpico_workflow_executes_with_real_binaries(tmp_path: Path) -> None:
    _require_commands("simgmedian2d", "simgrichardsonlucy2d")

    module = _load_module(_example("sairpico_restoration_smoke"))
    wf, terminal = module.build_workflow(storage_path=str(tmp_path / "sairpico"))

    result = wf.compute(terminal)

    assert len(result) == 1
    assert Path(result.iloc[0]["output_image"]).exists()
    assert Path(result.iloc[0]["output_image_1"]).exists()


@pytest.mark.public_data
@pytest.mark.external_binary
@pytest.mark.model_runtime
def test_fish_public_cil_workflow_executes_when_downloads_are_allowed(
    tmp_path: Path,
) -> None:
    if os.environ.get("BIOIMAGEFLOW_ALLOW_PUBLIC_DOWNLOADS") != "1":
        pytest.skip("set BIOIMAGEFLOW_ALLOW_PUBLIC_DOWNLOADS=1 to download CIL data")
    _require_commands("atlas")
    _require_modules("cellpose", "SimpleITK")

    module = _load_module(_example("fish_analysis"))
    wf, terminal = module.build_fish_workflow(
        storage_path=str(tmp_path / "fish_public" / "bif"),
        data_dir=str(tmp_path / "fish_public" / "data"),
    )
    wf.use_wetlands = False

    result = wf.compute(terminal)

    assert not result.empty
    assert {"avg_spots_fols2", "avg_spots_csfr1"} & set(result.columns)


@pytest.mark.slow
@pytest.mark.parametrize(
    ("workflow_name", "artifact_column"),
    [
        ("puncta_analysis", "summary_csv"),
        ("restoration_benchmark", "metrics_csv"),
        ("tracking_analysis", "metrics_csv"),
    ],
)
def test_specialized_workflow_acceptance_smoke(
    tmp_path: Path,
    workflow_name: str,
    artifact_column: str,
) -> None:
    module = _load_module(_example(workflow_name))
    wf, terminal = module.build_workflow(storage_path=str(tmp_path / workflow_name))
    wf.use_wetlands = False

    result = wf.compute(terminal)

    artifact = Path(result.iloc[0][artifact_column])
    assert artifact.exists()
    if artifact.suffix == ".csv":
        assert not pd.read_csv(artifact).empty
