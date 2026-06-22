from __future__ import annotations

import os
from pathlib import Path

import imageio.v3 as iio
import numpy as np
import pytest

from tests.workflow_catalog.test_workflows import (
    _load_module,
    _write_cell_counting_input,
    _write_restoration_pair,
)


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


def _write_sairpico_input(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    yy, xx = np.mgrid[0:32, 0:32]
    image = np.exp(-(((yy - 16) ** 2 + (xx - 16) ** 2) / 42.0)).astype(np.float32)
    image += 0.08 * np.exp(-(((yy - 10) ** 2 + (xx - 23) ** 2) / 8.0)).astype(np.float32)
    iio.imwrite(path, image)
    return path


@pytest.mark.public_data
@pytest.mark.model_runtime
def test_bbbc038_benchmark_public_model_runtime_smoke(
    tmp_path: Path,
    complete_wetlands_config: dict,
) -> None:
    data_dir = os.environ.get("BIOIMAGEFLOW_BBBC038_DATA_DIR")
    if data_dir is None:
        pytest.skip("set BIOIMAGEFLOW_BBBC038_DATA_DIR to a BBBC038 stage1_train subset")

    module = _load_module(_example("bbbc038_segmentation_benchmark"))
    wf, terminal = module.build_workflow(
        storage_path=str(tmp_path / "bbbc038_benchmark"),
        data_dir=data_dir,
        engine="wetlands",
        wetlands_config=complete_wetlands_config,
    )

    result = wf.compute(terminal)

    assert {"cellpose3", "cellpose_sam", "stardist", "classical_threshold"} == set(
        result["method"]
    )
    assert all(Path(path).exists() for path in result["predicted_label_image"])


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
        storage_path=str(tmp_path / "atlas_parameter_sweep" / "bif"), engine="wetlands",
        wetlands_config=complete_wetlands_config,
    )

    result = wf.compute(terminal)

    assert len(result) == 6
    assert int(result.iloc[0]["image_count"]) == 6
    assert Path(result.iloc[0]["mosaic_path"]).exists()
    assert {"label_count", "object_pixel_count", "foreground_fraction"} <= set(result.columns)


@pytest.mark.external_binary
@pytest.mark.sairpico_binary
def test_sairpico_workflow_executes_with_real_binaries(
    tmp_path: Path,
    complete_wetlands_config: dict,
) -> None:
    input_image = os.environ.get("BIOIMAGEFLOW_SAIRPICO_INPUT_IMAGE")
    if input_image is None:
        pytest.skip("set BIOIMAGEFLOW_SAIRPICO_INPUT_IMAGE to run SAIRPICO binary validation")

    module = _load_module(_example("sairpico_deconvolution"))
    wf, terminal = module.build_workflow(
        input_image=input_image,
        storage_path=str(tmp_path / "sairpico"), engine="wetlands",
        wetlands_config=complete_wetlands_config,
    )

    result = wf.compute(terminal)

    assert len(result) == 1
    assert Path(result.iloc[0]["psf_image"]).exists()
    assert Path(result.iloc[0]["denoised_image"]).exists()
    assert Path(result.iloc[0]["deconvolved_image"]).exists()


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
        data_dir=str(tmp_path / "fish_public" / "data"), engine="wetlands",
        wetlands_config=complete_wetlands_config,
    )

    result = wf.compute(terminal)

    assert not result.empty
    assert {
        "avg_fols2_per_nucleus",
        "avg_csf1r_per_nucleus",
        "total_nuclei",
    } <= set(result.columns)


@pytest.mark.slow
@pytest.mark.parametrize(
    ("workflow_name", "artifact_column"),
    [
        ("cell_counting_phenotyping", None),
        pytest.param(
            "low_snr_restoration",
            "restored_image",
            marks=pytest.mark.model_runtime,
        ),
        pytest.param("live_cell_tracking", None, marks=pytest.mark.model_runtime),
    ],
)
def test_specialized_workflow_acceptance_smoke(
    tmp_path: Path,
    workflow_name: str,
    artifact_column: str,
    complete_wetlands_config: dict,
) -> None:
    module = _load_module(_example(workflow_name))
    kwargs = {
        "storage_path": str(tmp_path / workflow_name),
        "engine": "wetlands",
        "wetlands_config": complete_wetlands_config,
    }
    if workflow_name == "cell_counting_phenotyping":
        kwargs["input_image"] = str(_write_cell_counting_input(tmp_path / "bbbc038_crop.tif"))
    if workflow_name == "low_snr_restoration":
        checkpoint_env = os.environ.get("BIOIMAGEFLOW_CAREAMICS_CHECKPOINT")
        if checkpoint_env is None:
            pytest.skip("set BIOIMAGEFLOW_CAREAMICS_CHECKPOINT to run CAREamics model-runtime validation")
        clean_image, degraded_image = _write_restoration_pair(tmp_path / "restoration_data")
        kwargs["clean_image"] = str(clean_image)
        kwargs["degraded_image"] = str(degraded_image)
        kwargs["checkpoint"] = checkpoint_env
    if workflow_name == "live_cell_tracking":
        label_image = os.environ.get("BIOIMAGEFLOW_CTC_LABEL_IMAGE")
        if label_image is None:
            pytest.skip("set BIOIMAGEFLOW_CTC_LABEL_IMAGE to run live-cell tracking model-runtime validation")
        kwargs["label_image"] = label_image
    wf, terminal = module.build_workflow(**kwargs)

    result = wf.compute(terminal)

    assert not result.empty
    if artifact_column is not None:
        artifact = Path(result.iloc[0][artifact_column])
        assert artifact.exists()
