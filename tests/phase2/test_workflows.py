from pathlib import Path

import importlib.util
import imageio.v3 as iio
import numpy as np
import pandas as pd
import pytest

from bioimageflow_sairpico_tools import MedianDenoising, RichardsonLucyDeconvolution
from bioimageflow_core import Arguments


def _load_module(path: Path):
    spec = importlib.util.spec_from_file_location(path.parent.name, path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _example(name: str) -> Path:
    root = Path(__file__).resolve().parents[2]
    return root / "example-workflows" / name / "workflow.py"


def test_fish_heavy_workflow_constructs_with_package_imports(tmp_path: Path) -> None:
    module = _load_module(_example("fish_analysis"))

    wf, terminal = module.build_fish_workflow(
        storage_path=str(tmp_path / "fish_heavy" / "bif"),
        data_dir=str(tmp_path / "fish_heavy" / "data"),
    )

    assert terminal.name == "avg_spots_per_nucleus"
    assert {
        "atlas_fols2",
        "atlas_csfr1",
        "cellpose3_nuclei",
        "avg_spots_per_nucleus",
    } <= set(wf.nodes)


def test_synthetic_fish_workflow_executes(tmp_path: Path) -> None:
    module = _load_module(_example("fish_analysis"))

    wf, terminal = module.build_synthetic_fish_workflow(
        storage_path=str(tmp_path / "fish_synthetic")
    )
    result = wf.compute(terminal)

    assert {"summary_csv", "label_count", "summary_csv_1", "label_count_1"} <= set(
        result.columns
    )
    assert not result.empty
    assert int(result["label_count"].iloc[0]) >= 1
    assert int(result["label_count_1"].iloc[0]) >= 1
    assert not pd_read(result["summary_csv"].iloc[0]).empty
    assert not pd_read(result["summary_csv_1"].iloc[0]).empty


def test_bbbc038_segmentation_benchmark_constructs_and_executes(tmp_path: Path) -> None:
    module = _load_module(_example("bbbc038_segmentation_benchmark"))

    wf, terminal = module.build_workflow(storage_path=str(tmp_path / "bbbc038"))
    assert {"threshold_nuclei", "benchmark_against_reference"} <= set(wf.nodes)

    result = wf.compute(terminal)
    assert len(result) == 1
    row = result.iloc[0]
    assert row["predicted_label_count"] == 2
    assert row["reference_label_count"] == 2
    assert row["foreground_iou"] > 0.95


def test_ome_normalization_executes_tiny_fixture(tmp_path: Path) -> None:
    module = _load_module(_example("ome_normalization"))

    wf, terminal = module.build_workflow(storage_path=str(tmp_path / "ome"))
    assert {
        "read_source",
        "select_channel_z",
        "write_ome_tiff",
        "write_ome_zarr",
        "collect_normalized_outputs",
    } <= set(wf.nodes)

    result = wf.compute(terminal)
    assert len(result) == 1
    ome_tiff = Path(result.iloc[0]["output_image"])
    ome_zarr = Path(result.iloc[0]["output_path"])
    assert ome_tiff.exists()
    assert ome_zarr.is_dir()
    assert (ome_zarr / ".zattrs").exists()
    assert iio.imread(ome_tiff).shape == (16, 18)


def test_sairpico_smoke_workflow_constructs_and_executes_with_fake_binary(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    module = _load_module(_example("sairpico_restoration_smoke"))
    calls: list[list[str]] = []

    def fake_run(command: list[object]) -> None:
        command = [str(value) for value in command]
        calls.append(command)
        output = Path(command[command.index("-o") + 1])
        input_path = Path(command[command.index("-i") + 1])
        output.parent.mkdir(parents=True, exist_ok=True)
        if input_path.exists():
            iio.imwrite(
                output,
                np.asarray(iio.imread(input_path), dtype=np.float32),
                extension=".tif",
            )
        else:
            output.touch()

    monkeypatch.setitem(module.MedianDenoising.process_row.__globals__, "_run", fake_run)
    monkeypatch.setitem(
        module.RichardsonLucyDeconvolution.process_row.__globals__, "_run", fake_run
    )

    wf, terminal = module.build_workflow(storage_path=str(tmp_path / "sairpico"))
    assert {
        "median_denoise_2d",
        "richardson_lucy_2d",
        "collect_sairpico_outputs",
    } <= set(wf.nodes)

    result = wf.compute(terminal)
    assert len(result) == 1
    assert Path(result.iloc[0]["output_image"]).exists()
    assert Path(result.iloc[0]["output_image_1"]).exists()
    assert {call[0] for call in calls} == {"simgmedian2d", "simgrichardsonlucy2d"}
    assert calls[1][calls[1].index("-i") + 1] == result.iloc[0]["output_image"]


def test_sairpico_command_construction_without_binaries(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: list[list[str]] = []

    def fake_run(command: list[object]) -> None:
        calls.append([str(value) for value in command])

    monkeypatch.setitem(MedianDenoising.process_row.__globals__, "_run", fake_run)
    monkeypatch.setitem(
        RichardsonLucyDeconvolution.process_row.__globals__, "_run", fake_run
    )
    image = tmp_path / "input.tif"
    image.touch()

    MedianDenoising().process_row(Arguments(
        input_image=image,
        output_image=tmp_path / "median.tif",
        denoising_type="2D",
        radius_x=1,
        radius_y=2,
        radius_z=1,
        radius_t=1,
        padding=True,
    ))
    RichardsonLucyDeconvolution().process_row(Arguments(
        input_image=image,
        output_image=tmp_path / "rl.tif",
        deconvolution_type="2D",
        sigma=1.2,
        psf_image=None,
        niter=3,
        regularization_lambda=0.01,
        padding=True,
    ))

    assert calls == [
        [
            "simgmedian2d",
            "-i",
            str(image),
            "-o",
            str(tmp_path / "median.tif"),
            "-rx",
            "1",
            "-ry",
            "2",
            "-padding",
            "true",
        ],
        [
            "simgrichardsonlucy2d",
            "-i",
            str(image),
            "-o",
            str(tmp_path / "rl.tif"),
            "-niter",
            "3",
            "-padding",
            "true",
            "-sigma",
            "1.2",
            "-lambda",
            "0.01",
        ],
    ]


def test_bbbc038_public_data_path_is_documented() -> None:
    docs = (Path(__file__).resolve().parents[2] / "docs/source/phase2/index.rst").read_text()
    assert "BBBC038" in docs
    assert "Broad Bioimage" in docs
    assert "Benchmark Collection" in docs


def pd_read(path: str) -> pd.DataFrame:
    return pd.read_csv(path)
