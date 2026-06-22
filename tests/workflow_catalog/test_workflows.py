from pathlib import Path
from typing import Any, cast

import importlib.util
import sys
import types
import imageio.v3 as iio
import numpy as np
import pandas as pd
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


def _write_tiny_bbbc038_sample(
    data_dir: Path,
    sample_id: str,
    *,
    rgb: bool = False,
) -> Path:
    sample_dir = data_dir / "stage1_train" / sample_id
    images_dir = sample_dir / "images"
    masks_dir = sample_dir / "masks"
    images_dir.mkdir(parents=True, exist_ok=True)
    masks_dir.mkdir(parents=True, exist_ok=True)

    yy, xx = np.mgrid[0:64, 0:64]
    image = np.zeros((64, 64), dtype=np.float32)
    objects = [(20, 22, 8), (42, 40, 10)]
    for index, (cy, cx, radius) in enumerate(objects, start=1):
        mask = (yy - cy) ** 2 + (xx - cx) ** 2 <= radius**2
        image[mask] = 1.0
        iio.imwrite(masks_dir / f"mask_{index}.tif", mask.astype(np.uint8) * 255)
    image += np.linspace(0.0, 0.1, image.shape[1], dtype=np.float32)
    if rgb:
        rgb_image = np.stack([image, image * 0.7, image * 0.4], axis=-1)
        iio.imwrite(images_dir / f"{sample_id}.tif", rgb_image)
    else:
        iio.imwrite(images_dir / f"{sample_id}.tif", image)
    return sample_dir


def _write_tiny_bbbc038_subset(data_dir: Path, *, sample_count: int = 1) -> Path:
    _write_tiny_bbbc038_sample(data_dir, "tiny_bbbc038_1")
    if sample_count > 1:
        _write_tiny_bbbc038_sample(data_dir, "tiny_bbbc038_2", rgb=True)
    return data_dir


def _write_cell_counting_input(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    yy, xx = np.mgrid[0:64, 0:64]
    image = np.zeros((64, 64), dtype=np.float32)
    for cy, cx, radius, value in [(18, 18, 7, 0.8), (42, 25, 9, 1.0), (36, 47, 6, 0.7)]:
        image[(yy - cy) ** 2 + (xx - cx) ** 2 <= radius**2] = value
    iio.imwrite(path, image)
    return path


def _write_restoration_pair(data_dir: Path) -> tuple[Path, Path]:
    data_dir.mkdir(parents=True, exist_ok=True)
    yy, xx = np.mgrid[0:48, 0:48]
    clean = np.zeros((48, 48), dtype=np.float32)
    clean[(yy - 24) ** 2 + (xx - 24) ** 2 <= 11**2] = 1.0
    clean[(yy - 16) ** 2 + (xx - 16) ** 2 <= 4**2] = 0.6
    degraded = np.clip(clean * 0.8 + 0.05, 0.0, 1.0)
    clean_path = data_dir / "low_snr_clean_crop.tif"
    degraded_path = data_dir / "low_snr_degraded_crop.tif"
    iio.imwrite(clean_path, clean)
    iio.imwrite(degraded_path, degraded.astype(np.float32))
    return clean_path, degraded_path


def _install_fake_model_runtimes(monkeypatch: pytest.MonkeyPatch) -> None:
    cellpose_module = cast(Any, types.ModuleType("cellpose"))
    cellpose_models_module = cast(Any, types.ModuleType("cellpose.models"))

    class FakeCellpose:
        def __init__(self, model_type: str) -> None:
            self.model_type = model_type

        def eval(self, image: np.ndarray, **_: object) -> tuple[np.ndarray, None, None, None]:
            mask = np.zeros(image.shape[-2:], dtype=np.uint32)
            mask[14:27, 16:29] = 1
            mask[34:51, 32:49] = 2
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
            labels[14:27, 16:29] = 1
            labels[34:51, 32:49] = 2
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
        "extract_ch2_nuclei",
        "fols2_marker_spot_analysis",
        "csf1r_marker_spot_analysis",
        "cellpose3_nuclei",
        "avg_spots_per_nucleus",
    } <= set(wf.nodes)
    binding = wf.nodes["cellpose3_nuclei"]._column_bindings["input_image"]
    assert binding.node.name == "extract_ch2_nuclei"
    assert binding.column == "output_image"


def test_fish_public_module_exposes_one_canonical_workflow() -> None:
    module = _load_module(_example("fish_analysis"))

    public_builders = [
        name for name in dir(module)
        if name.startswith("build_") and name.endswith("_workflow")
    ]
    assert public_builders == ["build_fish_workflow"]


def test_fish_public_contract_uses_csf1r_spelling() -> None:
    root = Path(__file__).resolve().parents[2]
    public_paths = [
        root / "example-workflows/fish_analysis/workflow.py",
        root / "example-workflows/fish_analysis/expected_outputs.yml",
        root / "example-workflows/fish_analysis/data_manifest.yml",
        root / "docs/source/workflows/index.rst",
    ]
    combined = "\n".join(path.read_text() for path in public_paths)

    assert "csfr1" not in combined.lower()
    assert "avg_csf1r_per_nucleus" in combined
    assert "total_nuclei_csf1r" in combined
    assert "total_csf1r_spots" in combined


def test_fish_average_spots_includes_nuclei_with_zero_marker_spots() -> None:
    module = _load_module(
        Path(__file__).resolve().parents[2]
        / "example-workflows/fish_analysis/tools/average_spots_per_nucleus.py"
    )
    tool = module.AverageSpotsPerNucleus()
    fols2 = pd.DataFrame(
        {
            "reference_label": [1, 2],
            "spot_label": [3, 0],
            "overlap_count": [5, 20],
        },
        index=["image_a::0", "image_a::1"],
    )
    csf1r = pd.DataFrame(
        {
            "reference_label": [1, 2],
            "spot_label": [0, 4],
            "overlap_count": [20, 8],
        },
        index=["image_a::0", "image_a::1"],
    )

    result = tool.merge_dataframes([fols2, csf1r], arguments={})

    assert result.iloc[0]["total_nuclei"] == 2
    assert result.iloc[0]["total_nuclei_fols2"] == 1
    assert result.iloc[0]["total_nuclei_csf1r"] == 1
    assert result.iloc[0]["avg_fols2_per_nucleus"] == 0.5
    assert result.iloc[0]["avg_csf1r_per_nucleus"] == 0.5


@pytest.mark.acceptance
def test_bbbc038_segmentation_benchmark_constructs_and_executes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _install_fake_model_runtimes(monkeypatch)
    module = _load_module(_example("bbbc038_segmentation_benchmark"))
    data_dir = _write_tiny_bbbc038_subset(tmp_path / "bbbc038_data", sample_count=2)

    wf, terminal = module.build_workflow(
        storage_path=str(tmp_path / "bbbc038"),
        data_dir=str(data_dir),
        engine="direct",
    )
    assert {
        "bbbc038_samples",
        "build_reference_labels",
        "prepare_segmentation_images",
        "cellpose3_segmentation",
        "cellpose_sam_segmentation",
        "stardist_segmentation",
        "classical_threshold_segmentation",
        "benchmark_cellpose3",
        "benchmark_cellpose_sam",
        "benchmark_stardist",
        "benchmark_classical_threshold",
        "bbbc038_benchmark_metrics",
    } <= set(wf.nodes)
    assert "benchmark_segmentation_methods" not in wf.nodes

    result = wf.compute(terminal)
    assert len(result) == 8
    assert set(result["method"]) == {
        "cellpose3",
        "cellpose_sam",
        "stardist",
        "classical_threshold",
    }
    assert result.groupby("input_image")["method"].nunique().to_list() == [4, 4]
    assert result["predicted_label_count"].min() >= 1
    assert result["reference_label_count"].min() == 2
    assert result["foreground_iou"].min() > 0.25
    assert all(Path(path).exists() for path in result["predicted_label_image"])
    assert all(Path(path).exists() for path in result["reference_label_image"])
    assert all(Path(path).exists() for path in result["overlay_image"])


def test_bbbc038_workflow_source_has_no_collapsed_benchmark_tool() -> None:
    source = _example("bbbc038_segmentation_benchmark").read_text()

    assert "BBBC038SegmentationComparison" not in source
    assert "class Synthetic" not in source
    assert "masks = {" not in source


def test_cell_counting_workflow_consumes_supplied_image_and_measures_features(
    tmp_path: Path,
) -> None:
    module = _load_module(_example("cell_counting_phenotyping"))
    input_image = _write_cell_counting_input(tmp_path / "bbbc038_crop.tif")

    wf, terminal = module.build_workflow(
        input_image=str(input_image),
        storage_path=str(tmp_path / "cell_counting"),
        engine="direct",
    )

    assert terminal.name == "summarize_phenotypes"
    assert {
        "segment_cells",
        "measure_regions",
        "measure_shape_features",
        "measure_intensity_features",
        "region_shape_table",
        "object_feature_table",
        "summarize_phenotypes",
    } <= set(wf.nodes)
    result = wf.compute(terminal)
    assert {
        "object_count",
        "mean_area",
        "mean_intensity",
        "mean_perimeter",
        "mean_equivalent_diameter",
    } <= set(result.columns)
    assert int(result.iloc[0]["object_count"]) >= 1


def test_cell_counting_public_workflow_does_not_generate_fixture() -> None:
    source = _example("cell_counting_phenotyping").read_text()

    assert "_write_fixture" not in source
    assert "synthetic_cell_counting" not in source


def test_low_snr_restoration_consumes_supplied_images_and_writes_preview(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    module = _load_module(_example("low_snr_restoration"))
    clean_image, degraded_image = _write_restoration_pair(tmp_path / "restoration_data")
    expected_checkpoint = tmp_path / "careamics_checkpoint.ckpt"
    expected_checkpoint.write_text("fake checkpoint")
    careamics_module = types.ModuleType("careamics")

    def fake_predict(image: Any, *, checkpoint: Path | None) -> Any:
        assert checkpoint == expected_checkpoint
        return iio.imread(clean_image).astype(np.float32)

    careamics_module.predict = fake_predict
    monkeypatch.setitem(sys.modules, "careamics", careamics_module)

    wf, terminal = module.build_workflow(
        clean_image=str(clean_image),
        degraded_image=str(degraded_image),
        checkpoint=str(expected_checkpoint),
        storage_path=str(tmp_path / "low_snr"),
        engine="direct",
    )

    assert {
        "careamics_n2v_restoration",
        "evaluate_restoration",
        "restoration_preview",
        "restoration_results",
    } <= set(wf.nodes)
    assert "low_snr_fixture" not in wf.nodes
    result = wf.compute(terminal)
    assert result.iloc[0]["mse_restored"] < result.iloc[0]["mse_degraded"]
    assert Path(result.iloc[0]["restored_image"]).exists()
    assert Path(result.iloc[0]["preview_image"]).exists()


def test_low_snr_public_workflow_does_not_generate_fixture() -> None:
    source = _example("low_snr_restoration").read_text()

    assert "LowSNRFixture" not in source
    assert "low_snr_fixture" not in source
    assert "Generated" not in source


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
        module.AtlasSpotDetection.process_row.__globals__,
        "run_external_command_with_staged_output",
        fake_run,
    )

    wf, terminal = module.build_parameter_space_workflow(
        data_dir=str(data_dir),
        storage_path=str(tmp_path / "atlas_parameter_sweep" / "bif"),
        engine="direct",
    )
    assert wf.engine_type == "direct"
    result = wf.compute(terminal)
    assert len(result) == 6
    assert int(result.iloc[0]["image_count"]) == 6
    assert Path(result.iloc[0]["mosaic_path"]).exists()
    assert len(calls) == 6
    assert {call[0] for call in calls} == {"atlas"}


def test_canonical_fish_workflow_contains_marker_sub_workflow_nodes(
    tmp_path: Path,
) -> None:
    module = _load_module(_example("fish_analysis"))
    wf, terminal = module.build_fish_workflow(
        storage_path=str(tmp_path / "fish_canonical" / "bif"),
        data_dir=str(tmp_path / "fish_canonical" / "data"),
    )
    assert terminal.name == "avg_spots_per_nucleus"
    assert {
        "download_cil_images",
        "extract_ch2_nuclei",
        "cellpose3_nuclei",
        "fols2_marker_spot_analysis",
        "csf1r_marker_spot_analysis",
        "avg_spots_per_nucleus",
    } <= set(wf.nodes)
    assert "read_image" not in wf.nodes


def test_marker_spot_analysis_is_real_atlas_subworkflow() -> None:
    source = (
        Path(__file__).resolve().parents[2]
        / "example-workflows/fish_analysis/tools/marker_spot_analysis.py"
    ).read_text()

    assert "class MarkerSpotAnalysis(SubWorkflow)" in source
    assert "ExtractChannel()" in source
    assert "AtlasSpotDetection()" in source
    assert "ConnectedComponents()" in source
    assert "LabelOverlaps()" in source
    assert "DetectSpots" not in source
    assert "AssignSpotsToLabels" not in source


def test_sairpico_deconvolution_workflow_constructs_and_executes_with_fake_binary(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    module = _load_module(_example("sairpico_deconvolution"))
    calls: list[list[str]] = []

    def fake_run(command: list[object], output_path: Path) -> None:
        command_parts = [str(value) for value in command]
        calls.append(command_parts)
        output = Path(command_parts[command_parts.index("-o") + 1])
        assert output == output_path
        output.parent.mkdir(parents=True, exist_ok=True)
        if "-i" in command_parts:
            input_path = Path(command_parts[command_parts.index("-i") + 1])
            iio.imwrite(
                output,
                np.asarray(iio.imread(input_path), dtype=np.float32),
                extension=".tif",
                photometric="minisblack",
            )
        else:
            iio.imwrite(output, np.ones((5, 16, 16), dtype=np.float32), extension=".tif")

    monkeypatch.setitem(
        module.GaussianPSF.process_row.__globals__,
        "_run_with_staged_output",
        fake_run,
    )
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

    wf, terminal = module.build_workflow(
        storage_path=str(tmp_path / "sairpico"),
        engine="direct",
    )
    assert wf.engine_type == "direct"
    assert {
        "sairpico_gaussian_psf",
        "sairpico_median_denoise",
        "sairpico_richardson_lucy",
        "sairpico_deconvolution_metrics",
    } <= set(wf.nodes)

    result = wf.compute(terminal)
    assert len(result) == 1
    assert Path(result.iloc[0]["psf_image"]).exists()
    assert Path(result.iloc[0]["denoised_image"]).exists()
    assert Path(result.iloc[0]["deconvolved_image"]).exists()
    assert {call[0] for call in calls} == {
        "simggaussian3dpsf",
        "simgmedian2d",
        "simgrichardsonlucy2d",
    }
    assert result.iloc[0]["deconvolved_sharpness"] >= 0.0


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
        / "docs/source/workflows/index.rst"
    ).read_text()
    assert "BBBC038" in docs
    assert "Broad Bioimage" in docs
    assert "Benchmark Collection" in docs


def test_example_workflow_documentation_records_review_contract() -> None:
    root = Path(__file__).resolve().parents[2]
    docs = [
        root / "docs/source/workflows/index.rst",
        root / "example-workflows/fish_analysis/data_manifest.yml",
        root / "example-workflows/fish_analysis/expected_outputs.yml",
        root / "example-workflows/parameter_space_exploration/data_manifest.yml",
        root / "example-workflows/parameter_space_exploration/expected_outputs.yml",
        root / "example-workflows/bbbc038_segmentation_benchmark/data_manifest.yml",
        root / "example-workflows/bbbc038_segmentation_benchmark/expected_outputs.yml",
        root / "example-workflows/cell_counting_phenotyping/data_manifest.yml",
        root / "example-workflows/cell_counting_phenotyping/expected_outputs.yml",
        root / "example-workflows/low_snr_restoration/data_manifest.yml",
        root / "example-workflows/low_snr_restoration/expected_outputs.yml",
        root / "example-workflows/sairpico_deconvolution/data_manifest.yml",
        root / "example-workflows/sairpico_deconvolution/expected_outputs.yml",
        root / "example-workflows/live_cell_tracking/data_manifest.yml",
        root / "example-workflows/live_cell_tracking/expected_outputs.yml",
        root / "packages/bioimageflow-segmentation-tools/docs/workflows/bbbc038_segmentation_benchmark.md",
        root / "example-workflows/parameter_space_exploration/README.md",
    ]
    for doc_path in docs:
        text = doc_path.read_text()
        if doc_path.suffix in {".yml", ".yaml"}:
            assert "workflow:" in text
            assert "outputs:" in text or "data:" in text
        else:
            assert "Data" in text
            assert "Expected" not in text
            assert "Analysis " + "question" not in text
