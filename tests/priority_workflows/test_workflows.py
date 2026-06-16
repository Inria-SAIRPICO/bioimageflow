from pathlib import Path
from typing import Any, cast

import importlib.util
import sys
import types
import imageio.v3 as iio
import numpy as np
import pytest

from bioimageflow_core import Arguments
from bioimageflow_sairpico_tools import MedianDenoising, RichardsonLucyDeconvolution


def _load_module(path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(path.parent.name, path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _example(name: str) -> Path:
    root = Path(__file__).resolve().parents[2]
    return root / "example-workflows" / name / "workflow.py"


def _write_multichannel_input(data_dir: Path) -> Path:
    data_dir.mkdir(parents=True, exist_ok=True)
    image = np.zeros((3, 32, 32), dtype=np.uint16)
    image[2, 8:15, 8:15] = 300
    image[2, 19:26, 18:25] = 500
    image[0, 10, 10] = 200
    image[1, 22, 21] = 250
    image_path = data_dir / "synthetic_cyx.tif"
    iio.imwrite(image_path, image, photometric="minisblack")
    return image_path


def _install_fake_model_runtimes(monkeypatch: pytest.MonkeyPatch) -> None:
    cellpose_module = cast(Any, types.ModuleType("cellpose"))
    cellpose_models_module = cast(Any, types.ModuleType("cellpose.models"))

    class FakeCellpose:
        def __init__(self, model_type: str) -> None:
            self.model_type = model_type

        def eval(self, image: np.ndarray, **_: object) -> tuple[np.ndarray, None, None, None]:
            mask = np.zeros(image.shape[-2:], dtype=np.uint32)
            mask[8:15, 8:15] = 1
            mask[19:26, 18:25] = 2
            return mask, None, None, None

    cellpose_models_module.Cellpose = FakeCellpose
    cellpose_module.models = cellpose_models_module
    monkeypatch.setitem(sys.modules, "cellpose", cellpose_module)
    monkeypatch.setitem(sys.modules, "cellpose.models", cellpose_models_module)

    csbdeep_module = cast(Any, types.ModuleType("csbdeep"))
    csbdeep_utils_module = cast(Any, types.ModuleType("csbdeep.utils"))
    csbdeep_utils_module.normalize = lambda image, *_args, **_kwargs: image
    csbdeep_module.utils = csbdeep_utils_module
    monkeypatch.setitem(sys.modules, "csbdeep", csbdeep_module)
    monkeypatch.setitem(sys.modules, "csbdeep.utils", csbdeep_utils_module)

    stardist_module = cast(Any, types.ModuleType("stardist"))
    stardist_models_module = cast(Any, types.ModuleType("stardist.models"))

    class FakeStarDist2D:
        @classmethod
        def from_pretrained(cls, model_name: str) -> "FakeStarDist2D":
            return cls()

        def predict_instances(
            self, image: np.ndarray, **_: object
        ) -> tuple[np.ndarray, dict[str, object]]:
            labels = np.zeros(image.shape[-2:], dtype=np.uint32)
            labels[9:14, 9:14] = 1
            return labels, {}

    stardist_models_module.StarDist2D = FakeStarDist2D
    stardist_module.models = stardist_models_module
    monkeypatch.setitem(sys.modules, "stardist", stardist_module)
    monkeypatch.setitem(sys.modules, "stardist.models", stardist_models_module)


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

    assert {"label", "spot_count", "label_count", "label_1", "spot_count_1"} <= set(
        result.columns
    )
    assert not result.empty
    assert int(result["label_count"].iloc[0]) >= 1
    assert int(result["label_count_1"].iloc[0]) >= 1
    assert int(result["spot_count"].iloc[0]) >= 1
    assert int(result["spot_count_1"].iloc[0]) >= 1


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
        "convert_to_ome_tiff",
        "convert_to_ome_zarr",
        "collect_normalized_outputs",
    } <= set(wf.nodes)

    result = wf.compute(terminal)
    assert len(result) == 1
    ome_tiff = Path(result.iloc[0]["output_image"])
    ome_zarr = Path(result.iloc[0]["output_image_1"])
    assert ome_tiff.exists()
    assert ome_zarr.is_dir()
    assert (ome_zarr / ".zgroup").exists()
    assert (ome_zarr / ".zattrs").exists()
    expected = np.arange(2 * 3 * 16 * 18, dtype=np.uint16).reshape(2, 3, 16, 18)[1, 2]
    np.testing.assert_array_equal(iio.imread(ome_tiff), expected)


def test_cellpose_stardist_workflow_constructs_with_package_imports(
    tmp_path: Path,
) -> None:
    cellpose_stardist = _load_module(_example("cellpose3_stardist"))
    wf, cellpose, stardist = cellpose_stardist.build_segmentation_workflow(
        data_dir=str(tmp_path / "heavy_segmentation" / "data"),
        storage_path=str(tmp_path / "heavy_segmentation" / "bif"),
    )
    assert cellpose.name == "cellpose3_nuclei"
    assert stardist.name == "stardist_nuclei"
    assert {
        "input_images",
        "nuclei_channel",
        "cellpose3_nuclei",
        "stardist_nuclei",
    } <= set(wf.nodes)


def test_cellpose_stardist_workflow_executes_with_fake_model_runtimes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _install_fake_model_runtimes(monkeypatch)
    data_dir = tmp_path / "model_runtime" / "data"
    _write_multichannel_input(data_dir)

    module = _load_module(_example("cellpose3_stardist"))
    wf, cellpose, stardist = module.build_segmentation_workflow(
        data_dir=str(data_dir),
        storage_path=str(tmp_path / "model_runtime" / "bif"),
    )
    result = wf.compute(cellpose, stardist)
    cellpose_result = result["cellpose3_nuclei"]
    stardist_result = result["stardist_nuclei"]

    assert int(cellpose_result.iloc[0]["cell_count"]) == 2
    assert int(stardist_result.iloc[0]["object_count"]) == 1
    assert Path(cellpose_result.iloc[0]["mask"]).exists()
    assert Path(stardist_result.iloc[0]["mask"]).exists()


def test_parameter_space_workflow_constructs_with_package_imports(
    tmp_path: Path,
) -> None:
    parameter_space = _load_module(_example("parameter_space_exploration"))
    wf, terminal = parameter_space.build_parameter_space_workflow(
        data_dir=str(tmp_path / "atlas_parameter_sweep" / "data"),
        storage_path=str(tmp_path / "atlas_parameter_sweep" / "bif"),
    )
    assert terminal.name == "results_mosaic"
    assert {
        "input_images",
        "sensitivity_values",
        "size_values",
        "parameter_grid",
        "atlas_detections",
        "results_mosaic",
    } <= set(wf.nodes)


def test_parameter_space_workflow_executes_with_fake_atlas_binary(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    module = _load_module(_example("parameter_space_exploration"))
    data_dir = tmp_path / "atlas_parameter_sweep" / "data"
    data_dir.mkdir(parents=True)
    iio.imwrite(data_dir / "spots.tif", np.eye(16, dtype=np.uint8) * 255)

    calls: list[list[str]] = []

    def fake_run(command: list[object], **kwargs: object) -> None:
        command_parts = [str(value) for value in command]
        calls.append(command_parts)
        output = Path(str(kwargs["output_path"]))
        assert output == Path(command_parts[command_parts.index("-o") + 1])
        output.parent.mkdir(parents=True, exist_ok=True)
        iio.imwrite(output, np.eye(16, dtype=np.uint8) * 255)

    monkeypatch.setitem(
        module.Atlas.process_row.__globals__,
        "run_external_command_with_staged_output",
        fake_run,
    )

    wf, terminal = module.build_parameter_space_workflow(
        data_dir=str(data_dir),
        storage_path=str(tmp_path / "atlas_parameter_sweep" / "bif"),
    )
    result = wf.compute(terminal)
    assert len(result) == 6
    assert int(result.iloc[0]["image_count"]) == 6
    assert Path(result.iloc[0]["mosaic_path"]).exists()
    assert len(calls) == 6
    assert {call[0] for call in calls} == {"atlas"}


def test_fish_sub_workflow_constructs_with_package_imports(
    tmp_path: Path,
) -> None:
    fish_sub = _load_module(_example("fish_analysis_sub_workflows"))
    wf, terminal = fish_sub.build_fish_workflow(
        storage_path=str(tmp_path / "fish_sub" / "bif"),
        data_dir=str(tmp_path / "fish_sub" / "data"),
    )
    assert terminal.name == "avg_spots_per_nucleus"
    assert {
        "download_cil_images",
        "read_image",
        "cellpose3_nuclei",
        "fols2_analysis",
        "csfr1_analysis",
        "avg_spots_per_nucleus",
    } <= set(wf.nodes)


def test_sairpico_smoke_workflow_constructs_and_executes_with_fake_binary(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    module = _load_module(_example("sairpico_restoration_smoke"))
    calls: list[list[str]] = []

    def fake_run(command: list[object], output_path: Path) -> None:
        command_parts = [str(value) for value in command]
        calls.append(command_parts)
        output = Path(command_parts[command_parts.index("-o") + 1])
        assert output == output_path
        input_path = Path(command_parts[command_parts.index("-i") + 1])
        output.parent.mkdir(parents=True, exist_ok=True)
        if input_path.exists():
            iio.imwrite(
                output,
                np.asarray(iio.imread(input_path), dtype=np.float32),
                extension=".tif",
                photometric="minisblack",
            )
        else:
            output.touch()

    monkeypatch.setitem(
        module.MedianDenoising.process_row.__globals__,
        "_run_with_staged_output",
        fake_run,
    )
    monkeypatch.setitem(
        module.RichardsonLucyDeconvolution.process_row.__globals__,
        "_run_with_staged_output",
        fake_run,
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

    def fake_run(command: list[object], output_path: Path) -> None:
        assert Path(str(command[command.index("-o") + 1])) == output_path
        calls.append([str(value) for value in command])

    monkeypatch.setitem(
        MedianDenoising.process_row.__globals__,
        "_run_with_staged_output",
        fake_run,
    )
    monkeypatch.setitem(
        RichardsonLucyDeconvolution.process_row.__globals__,
        "_run_with_staged_output",
        fake_run,
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
    docs = (
        Path(__file__).resolve().parents[2]
        / "docs/source/priority_workflows/index.rst"
    ).read_text()
    assert "BBBC038" in docs
    assert "Broad Bioimage" in docs
    assert "Benchmark Collection" in docs


def test_example_workflow_documentation_records_review_contract() -> None:
    root = Path(__file__).resolve().parents[2]
    docs = [
        root / "docs/source/priority_workflows/index.rst",
        root / "docs/source/specialized_tool_workflows/index.rst",
        root / "packages/bioimageflow-io-tools/docs/workflows/ome_normalization.md",
        root / "packages/bioimageflow-segmentation-tools/docs/workflows/bbbc038_segmentation_benchmark.md",
        root / "packages/bioimageflow-sairpico-tools/docs/workflows/sairpico_restoration_smoke.md",
        root / "packages/bioimageflow-spot-tools/docs/workflows/puncta_analysis.md",
        root / "packages/bioimageflow-restoration-tools/docs/workflows/restoration_benchmark.md",
        root / "packages/bioimageflow-tracking-tools/docs/workflows/tracking_analysis.md",
        root / "example-workflows/parameter_space_exploration/README.md",
        root / "example-workflows/cellpose3_stardist/README.md",
        root / "example-workflows/fish_analysis_sub_workflows/README.md",
    ]
    for doc_path in docs:
        text = doc_path.read_text()
        assert "Analysis question" in text
        assert "Data" in text
        assert "Expected" in text
        assert "Test" in text
