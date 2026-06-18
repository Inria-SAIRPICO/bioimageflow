"""Tests for the BioImageFlow SAIRPICO tool wrappers."""

from __future__ import annotations

import sys
import ast
import importlib
import inspect
import json
from pathlib import Path

import imageio.v3 as iio
import numpy as np
import pandas as pd
import pytest

from bioimageflow import Workflow
from bioimageflow.env_manager import _find_tool_file
from bioimageflow.storage import Storage
from bioimageflow.validation import serialize_input_schema, serialize_output_schema
from bioimageflow_core import Arguments, ProcessingTool
from bioimageflow_core.worker import _load_module_from_file

from bioimageflow_sairpico_tools import (
    CImgDenoising,
    GaussianPSF,
    GibsonLanniPSF,
    HotspotDetection,
    HotspotToSpots,
    MedianDenoising,
    RichardsonLucyDeconvolution,
    SpitfireDeconvolution,
    WienerDeconvolution,
)
from bioimageflow_sairpico_tools import __all__ as exported_tools
from bioimageflow_sairpico_tools.tools import (
    _write_sairpico_environment_report,
    _write_sairpico_version_report,
)


ALL_TOOLS = [
    GaussianPSF,
    GibsonLanniPSF,
    RichardsonLucyDeconvolution,
    WienerDeconvolution,
    SpitfireDeconvolution,
    MedianDenoising,
    CImgDenoising,
    HotspotDetection,
    HotspotToSpots,
]

SAIRPICO_PACKAGE = Path(__file__).parents[1] / "bioimageflow_sairpico_tools"

pytestmark = pytest.mark.package_tools


@pytest.mark.parametrize("tool_cls", ALL_TOOLS)
def test_sairpico_tools_have_serializable_schemas(tool_cls: type[ProcessingTool]) -> None:
    assert issubclass(tool_cls, ProcessingTool)
    assert tool_cls.display_name
    assert tool_cls.documentation
    assert tool_cls.environment.name in {"simglib", "cimgdenoising", "hotspot"}

    inputs = serialize_input_schema(tool_cls)
    outputs = serialize_output_schema(tool_cls)

    assert isinstance(inputs, dict)
    assert isinstance(outputs, dict)
    assert outputs
    assert all("type" in field for field in outputs.values())


def test_legacy_lambda_parameter_is_renamed() -> None:
    richardson_schema = serialize_input_schema(RichardsonLucyDeconvolution)
    wiener_schema = serialize_input_schema(WienerDeconvolution)

    assert "regularization_lambda" in richardson_schema
    assert "regularization_lambda" in wiener_schema
    assert "lambda" not in richardson_schema
    assert "lambda" not in wiener_schema


def test_deconvolution_psf_schema_serializes_as_image_input() -> None:
    for tool_cls in [
        RichardsonLucyDeconvolution,
        WienerDeconvolution,
        SpitfireDeconvolution,
    ]:
        schema = serialize_input_schema(tool_cls)
        psf_schema = schema["psf_image"]

        assert psf_schema["type"] == "ImageFile"
        assert psf_schema["required"] is False
        assert psf_schema["default"] is None
        assert psf_schema["image_spec"]["semantics"] == ["intensity"]
        assert psf_schema["image_spec"]["formats"] == ["png", "tif", "tiff"]


def test_legacy_input_image_formats_are_declared() -> None:
    image_input_tools = [
        RichardsonLucyDeconvolution,
        WienerDeconvolution,
        SpitfireDeconvolution,
        MedianDenoising,
        CImgDenoising,
        HotspotDetection,
    ]

    for tool_cls in image_input_tools:
        schema = serialize_input_schema(tool_cls)
        assert schema["input_image"]["type"] == "ImageFile"
        assert schema["input_image"]["image_spec"]["formats"] == ["png", "tif", "tiff"]


def test_output_image_override_uses_output_template_not_input_binding(
    tmp_path: Path,
) -> None:
    input_path = tmp_path / "input.tif"
    iio.imwrite(input_path, np.zeros((8, 8), dtype=np.uint16))

    with Workflow(engine="direct", storage_path=tmp_path / "results"):
        node = MedianDenoising()(
            input_image=input_path,
            denoising_type="2D",
            output_templates={"output_image": "median.tif"},
            name="median",
        )

    assert node.output_templates == {"output_image": "median.tif"}


def test_importing_package_does_not_import_subprocess() -> None:
    modules_to_clear = [
        name for name in sys.modules
        if name == "bioimageflow_sairpico_tools"
        or name.startswith("bioimageflow_sairpico_tools.")
    ]
    for name in modules_to_clear:
        sys.modules.pop(name, None)
    sys.modules.pop("subprocess", None)

    __import__("bioimageflow_sairpico_tools")

    assert "subprocess" not in sys.modules


@pytest.mark.parametrize(
    "module_name",
    [
        "bioimageflow_sairpico_tools.simglib",
        "bioimageflow_sairpico_tools.cimgdenoising",
        "bioimageflow_sairpico_tools.hotspot",
    ],
)
def test_runtime_module_imports_do_not_mutate_sys_path(module_name: str) -> None:
    package_root = str(SAIRPICO_PACKAGE.parent)
    original_path = list(sys.path)
    try:
        sys.path[:] = [entry for entry in sys.path if entry != package_root]
        before = list(sys.path)
        sys.modules.pop(module_name, None)

        importlib.import_module(module_name)

        assert sys.path == before
    finally:
        sys.path[:] = original_path


@pytest.mark.parametrize(
    ("tool_cls", "module_name"),
    [
        (GaussianPSF, "bioimageflow_sairpico_tools.simglib"),
        (CImgDenoising, "bioimageflow_sairpico_tools.cimgdenoising"),
        (HotspotDetection, "bioimageflow_sairpico_tools.hotspot"),
    ],
)
def test_worker_loads_runtime_modules_as_packages(
    tool_cls: type[ProcessingTool],
    module_name: str,
) -> None:
    tool_file = _find_tool_file(tool_cls)
    config = json.loads(tool_file)
    assert config == {
        "mode": "module",
        "module": module_name,
        "sys_path": str(SAIRPICO_PACKAGE.parent),
    }

    original_path = list(sys.path)
    try:
        sys.path[:] = [entry for entry in sys.path if entry != config["sys_path"]]
        module = _load_module_from_file(tool_file)
        assert getattr(module, tool_cls.__name__).__name__ == tool_cls.__name__
    finally:
        sys.path[:] = original_path


def test_tools_are_isolated_by_runtime_module() -> None:
    assert Path(inspect.getfile(GaussianPSF)).name == "simglib.py"
    assert Path(inspect.getfile(GibsonLanniPSF)).name == "simglib.py"
    assert Path(inspect.getfile(RichardsonLucyDeconvolution)).name == "simglib.py"
    assert Path(inspect.getfile(WienerDeconvolution)).name == "simglib.py"
    assert Path(inspect.getfile(SpitfireDeconvolution)).name == "simglib.py"
    assert Path(inspect.getfile(MedianDenoising)).name == "simglib.py"
    assert Path(inspect.getfile(CImgDenoising)).name == "cimgdenoising.py"
    assert Path(inspect.getfile(HotspotDetection)).name == "hotspot.py"
    assert Path(inspect.getfile(HotspotToSpots)).name == "hotspot.py"


def test_sairpico_binary_environments_are_pinned_to_python39() -> None:
    for tool_cls in ALL_TOOLS:
        assert tool_cls.environment.dependencies["python"] == "3.9"


def _annotation_nodes(tree: ast.AST) -> list[ast.expr]:
    annotations: list[ast.expr] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.AnnAssign):
            annotations.append(node.annotation)
        elif isinstance(node, ast.arg) and node.annotation is not None:
            annotations.append(node.annotation)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.returns is not None:
            annotations.append(node.returns)
    return annotations


def test_python39_worker_modules_do_not_use_pep604_annotations() -> None:
    offenders: list[str] = []

    for source_path in [
        SAIRPICO_PACKAGE / "_common.py",
        SAIRPICO_PACKAGE / "simglib.py",
        SAIRPICO_PACKAGE / "hotspot.py",
        SAIRPICO_PACKAGE / "cimgdenoising.py",
    ]:
        source = source_path.read_text()
        tree = ast.parse(source, filename=str(source_path), feature_version=(3, 9))
        for annotation in _annotation_nodes(tree):
            if any(isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr) for node in ast.walk(annotation)):
                offenders.append(f"{source_path.relative_to(SAIRPICO_PACKAGE)}:{annotation.lineno}")

    assert offenders == []


def test_diagnostics_are_not_public_workflow_tools() -> None:
    import bioimageflow_sairpico_tools

    assert "ValidateSairpicoEnvironment" not in exported_tools
    assert "SairpicoVersionReport" not in exported_tools
    assert not hasattr(bioimageflow_sairpico_tools, "ValidateSairpicoEnvironment")
    assert not hasattr(bioimageflow_sairpico_tools, "SairpicoVersionReport")


def test_gaussian_psf_command(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls: list[tuple[list[str], bool]] = []

    def fake_run(command: list[str], run_kwargs: dict[str, object]) -> None:
        calls.append((command, bool(run_kwargs["check"])))
        output_path = Path(command[command.index("-o") + 1])
        output_path.write_text("psf")

    monkeypatch.setattr("bioimageflow_core.external._run_subprocess", fake_run)
    output = tmp_path / "psf.tif"

    result = GaussianPSF().process_row(Arguments(
        width=32,
        height=24,
        depth=8,
        sigmaxy=1.2,
        sigmaz=2.3,
        output_image=output,
    ))

    assert calls == [([
        "simggaussian3dpsf",
        "-o", calls[0][0][calls[0][0].index("-o") + 1],
        "-sigmaxy", "1.2",
        "-sigmaz", "2.3",
        "-depth", "8",
        "-height", "24",
        "-width", "32",
    ], True)]
    staged_output = Path(calls[0][0][calls[0][0].index("-o") + 1])
    assert staged_output != output
    assert output.read_text() == "psf"
    assert result.output_image == output


def test_richardson_lucy_2d_uses_regularization_lambda(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: list[list[str]] = []

    def fake_run(command: list[str], run_kwargs: dict[str, object]) -> None:
        assert run_kwargs["check"] is True
        calls.append(command)
        Path(command[command.index("-o") + 1]).write_text("deconvolved")

    monkeypatch.setattr("bioimageflow_core.external._run_subprocess", fake_run)
    input_image = tmp_path / "input.tif"
    output_image = tmp_path / "output.tif"

    RichardsonLucyDeconvolution().process_row(Arguments(
        input_image=input_image,
        deconvolution_type="2D",
        sigma=1.5,
        psf_image=None,
        niter=7,
        regularization_lambda=0.25,
        padding=True,
        output_image=output_image,
    ))

    assert calls == [[
        "simgrichardsonlucy2d",
        "-i", str(input_image),
        "-o", calls[0][calls[0].index("-o") + 1],
        "-niter", "7",
        "-padding", "true",
        "-sigma", "1.5",
        "-lambda", "0.25",
    ]]
    assert Path(calls[0][calls[0].index("-o") + 1]) != output_image
    assert output_image.read_text() == "deconvolved"


def test_wiener_3d_requires_existing_psf_before_subprocess(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import subprocess

    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: pytest.fail("subprocess.run should not be called"),
    )

    with pytest.raises(FileNotFoundError, match="psf_image"):
        WienerDeconvolution().process_row(Arguments(
            input_image=tmp_path / "input.tif",
            deconvolution_type="3D",
            sigma=1.5,
            psf_image=tmp_path / "missing_psf.tif",
            regularization_lambda=0.01,
            padding=False,
            output_image=tmp_path / "output.tif",
        ))


def test_spitfire_3d_command(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def fake_run(command: list[str], run_kwargs: dict[str, object]) -> None:
        assert run_kwargs["check"] is True
        calls.append(command)
        Path(command[command.index("-o") + 1]).write_text("spitfire")

    monkeypatch.setattr("bioimageflow_core.external._run_subprocess", fake_run)
    psf = tmp_path / "psf.tif"
    psf.touch()

    SpitfireDeconvolution().process_row(Arguments(
        input_image=tmp_path / "input.tif",
        deconvolution_type="3D",
        sigma=1.5,
        psf_image=psf,
        regularization=12.0,
        weighting=0.6,
        method="HV",
        padding=False,
        niter=150,
        output_image=tmp_path / "output.tif",
    ))

    assert calls == [[
        "simgspitfiredeconv3d",
        "-i", str(tmp_path / "input.tif"),
        "-o", calls[0][calls[0].index("-o") + 1],
        "-regularization", "12.0",
        "-weighting", "0.6",
        "-method", "HV",
        "-padding", "false",
        "-niter", "150",
        "-psf", str(psf),
    ]]
    assert Path(calls[0][calls[0].index("-o") + 1]) != tmp_path / "output.tif"
    assert (tmp_path / "output.tif").read_text() == "spitfire"


def test_median_4d_command(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def fake_run(command: list[str], run_kwargs: dict[str, object]) -> None:
        assert run_kwargs["check"] is True
        calls.append(command)
        Path(command[command.index("-o") + 1]).write_text("median")

    monkeypatch.setattr("bioimageflow_core.external._run_subprocess", fake_run)

    MedianDenoising().process_row(Arguments(
        input_image=tmp_path / "input.tif",
        denoising_type="4D",
        radius_x=2,
        radius_y=3,
        radius_z=4,
        radius_t=5,
        padding=True,
        output_image=tmp_path / "output.tif",
    ))

    assert calls == [[
        "simgmedian4d",
        "-i", str(tmp_path / "input.tif"),
        "-o", calls[0][calls[0].index("-o") + 1],
        "-rx", "2",
        "-ry", "3",
        "-padding", "true",
        "-rz", "4",
        "-rt", "5",
    ]]
    assert Path(calls[0][calls[0].index("-o") + 1]) != tmp_path / "output.tif"
    assert (tmp_path / "output.tif").read_text() == "median"


def test_cimg_denoising_command_omits_false_flags(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: list[list[str]] = []

    def fake_run(command: list[str], run_kwargs: dict[str, object]) -> None:
        assert run_kwargs["check"] is True
        calls.append(command)
        Path(command[command.index("-o") + 1]).write_text("cimg")

    monkeypatch.setattr("bioimageflow_core.external._run_subprocess", fake_run)

    CImgDenoising().process_row(Arguments(
        input_image=tmp_path / "input.tif",
        first=0,
        last=-1,
        alpha=0.0,
        scale=1.0,
        intensity_range=1.0,
        algorithm=None,
        gaussian_noise=0.0,
        poisson_noise=False,
        manual_sigma=0.0,
        stabilize_poisson=False,
        patch=3,
        neighborhood=7,
        denoising_parameter=-1.0,
        sparsity_parameter=0.6,
        iterations=4,
        output_image=tmp_path / "output.tif",
    ))

    command = calls[0]
    assert command[:2] == ["denoise", "-i"]
    assert "" not in command
    assert "-np" not in command
    assert "-stab" not in command
    assert "-algo" not in command
    assert "-range" in command
    assert Path(command[command.index("-o") + 1]) != tmp_path / "output.tif"
    assert (tmp_path / "output.tif").read_text() == "cimg"


def test_cimg_denoising_command_includes_algorithm_when_set(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: list[list[str]] = []

    def fake_run(command: list[str], run_kwargs: dict[str, object]) -> None:
        assert run_kwargs["check"] is True
        calls.append(command)
        Path(command[command.index("-o") + 1]).write_text("cimg")

    monkeypatch.setattr("bioimageflow_core.external._run_subprocess", fake_run)

    CImgDenoising().process_row(Arguments(
        input_image=tmp_path / "input.tif",
        first=0,
        last=-1,
        alpha=0.0,
        scale=1.0,
        intensity_range=1.0,
        algorithm="PEWA",
        gaussian_noise=0.0,
        poisson_noise=False,
        manual_sigma=0.0,
        stabilize_poisson=False,
        patch=3,
        neighborhood=7,
        denoising_parameter=-1.0,
        sparsity_parameter=0.6,
        iterations=4,
        output_image=tmp_path / "output.tif",
    ))

    command = calls[0]
    assert "" not in command
    assert command[-2:] == ["-algo", "PEWA"]
    assert "PEWA" in command
    assert Path(command[command.index("-o") + 1]) != tmp_path / "output.tif"
    assert (tmp_path / "output.tif").read_text() == "cimg"


def test_hotspot_detection_command(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def fake_run(command: list[str], run_kwargs: dict[str, object]) -> None:
        assert run_kwargs["check"] is True
        calls.append(command)
        Path(command[command.index("-o") + 1]).write_text("hotspot")

    monkeypatch.setattr("bioimageflow_core.external._run_subprocess", fake_run)

    HotspotDetection().process_row(Arguments(
        input_image=tmp_path / "input.tif",
        patch_size=3,
        neighborhood_size=5,
        p_value=0.2,
        output_image=tmp_path / "output.tif",
    ))

    assert calls == [[
        "hotSpotDetection",
        "-i", str(tmp_path / "input.tif"),
        "-o", calls[0][calls[0].index("-o") + 1],
        "-m", "3",
        "-n", "5",
        "-pv", "0.2",
    ]]
    assert Path(calls[0][calls[0].index("-o") + 1]) != tmp_path / "output.tif"
    assert (tmp_path / "output.tif").read_text() == "hotspot"


def test_validate_sairpico_environment_reports_missing_binaries(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import shutil

    monkeypatch.setattr(
        shutil,
        "which",
        lambda binary: f"/fake/bin/{binary}" if binary == "simgmedian2d" else None,
    )

    result = _write_sairpico_environment_report(
        binaries=["simgmedian2d", "hotSpotDetection"],
        report_csv=tmp_path / "environment.csv",
    )

    table = pd.read_csv(result["report_csv"]).sort_values("binary")
    assert table["available"].tolist() == [False, True]
    assert result["available_count"] == 1
    assert result["missing_count"] == 1
    assert result["ready"] is False


def test_sairpico_version_report_captures_versions(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: list[list[str]] = []

    class Completed:
        returncode = 0
        stdout = "simgmedian2d 0.1.2\n"
        stderr = ""

    def fake_run(command: list[str], **kwargs: object) -> Completed:
        calls.append(command)
        return Completed()

    import subprocess

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = _write_sairpico_version_report(
        binaries=["simgmedian2d"],
        version_argument="--version",
        report_csv=tmp_path / "versions.csv",
    )

    assert calls == [["simgmedian2d", "--version"]]
    table = pd.read_csv(result["report_csv"])
    assert table.loc[0, "binary"] == "simgmedian2d"
    assert table.loc[0, "version"] == "simgmedian2d 0.1.2"
    assert result["reported_count"] == 1


def test_sairpico_version_report_treats_nonzero_returncode_as_failed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class Completed:
        returncode = 2
        stdout = ""
        stderr = "failed but noisy\n"

    def fake_run(command: list[str], **kwargs: object) -> Completed:
        return Completed()

    import subprocess

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = _write_sairpico_version_report(
        binaries=["hotSpotDetection"],
        version_argument="--version",
        report_csv=tmp_path / "versions.csv",
    )

    table = pd.read_csv(result["report_csv"])
    assert table.loc[0, "returncode"] == 2
    assert table.loc[0, "version"] == "failed but noisy"
    assert result["reported_count"] == 0
    assert result["failed_count"] == 1


def test_hotspot_to_spots_converts_components_to_coordinate_table(tmp_path: Path) -> None:
    hotspot = np.zeros((12, 12), dtype=np.float32)
    hotspot[2:4, 2:4] = 5.0
    hotspot[8, 9] = 9.0
    hotspot_path = tmp_path / "hotspot.tif"
    iio.imwrite(hotspot_path, hotspot)

    result = HotspotToSpots().process_row(Arguments(
        hotspot_image=hotspot_path,
        threshold=1.0,
    ))

    assert result[0].spot_count == 2
    assert [row.intensity for row in result] == [5.0, 9.0]
    assert [row.area for row in result] == [4, 1]
    assert [(round(row.y, 2), round(row.x, 2)) for row in result] == [
        (2.5, 2.5),
        (8.0, 9.0),
    ]


def test_hotspot_to_spots_returns_no_rows_for_blank_image(tmp_path: Path) -> None:
    hotspot_path = tmp_path / "blank_hotspot.tif"
    iio.imwrite(hotspot_path, np.zeros((8, 8), dtype=np.float32))

    result = HotspotToSpots().process_row(Arguments(
        hotspot_image=hotspot_path,
        threshold=1.0,
    ))

    assert result == []
    assert HotspotToSpots.zero_row_scalar_outputs == {"spot_count": 0}


def test_hotspot_to_spots_publishes_zero_spot_count_metadata(tmp_path: Path) -> None:
    hotspot_path = tmp_path / "blank_hotspot.tif"
    iio.imwrite(hotspot_path, np.zeros((8, 8), dtype=np.float32))
    storage_path = tmp_path / "results"

    with Workflow(engine="direct", storage_path=storage_path) as wf:
        node = HotspotToSpots()(hotspot_image=hotspot_path, threshold=1.0)
        result = wf.compute(node)
        node_name = node.name

    assert result.empty
    assert list(result.columns) == [
        "spot_id",
        "y",
        "x",
        "intensity",
        "score",
        "area",
        "label",
        "spot_count",
    ]
    [run_dir] = [path for path in (storage_path / "runs").iterdir() if path.is_dir()]
    run_result = json.loads((run_dir / "nodes" / node_name / "result.json").read_text())
    result_key = run_result["result_key"]
    storage = Storage(storage_path)
    pointer = storage.load_current(result_key)
    assert pointer is not None
    record_dir = storage.result_dir(result_key) / "records" / pointer.record_id
    manifest = json.loads((record_dir / "manifest.json").read_text())
    assert manifest["outputs"] == [
        {
            "kind": "scalar_output",
            "output_column": "spot_count",
            "row_index": "0",
            "value": {"kind": "signed_integer", "value": "0"},
        }
    ]

    assert run_result["outputs"] == manifest["outputs"]
