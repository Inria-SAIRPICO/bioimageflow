"""Tests for the BioImageFlow SAIRPICO tool wrappers."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from bioimageflow.validation import serialize_input_schema, serialize_output_schema
from bioimageflow_core import Arguments, ProcessingTool

from bioimageflow_sairpico_tools import (
    CImgDenoising,
    GaussianPSF,
    GibsonLanniPSF,
    HotspotDetection,
    MedianDenoising,
    RichardsonLucyDeconvolution,
    SpitfireDeconvolution,
    WienerDeconvolution,
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
]


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


def test_gaussian_psf_command(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls: list[tuple[list[str], bool]] = []

    def fake_run(command: list[str], *, check: bool) -> None:
        calls.append((command, check))

    import subprocess

    monkeypatch.setattr(subprocess, "run", fake_run)
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
        "-o", str(output),
        "-sigmaxy", "1.2",
        "-sigmaz", "2.3",
        "-depth", "8",
        "-height", "24",
        "-width", "32",
    ], True)]
    assert result.output_image == output


def test_richardson_lucy_2d_uses_regularization_lambda(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: list[list[str]] = []

    def fake_run(command: list[str], *, check: bool) -> None:
        assert check is True
        calls.append(command)

    import subprocess

    monkeypatch.setattr(subprocess, "run", fake_run)
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
        "-o", str(output_image),
        "-niter", "7",
        "-padding", "true",
        "-sigma", "1.5",
        "-lambda", "0.25",
    ]]


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

    def fake_run(command: list[str], *, check: bool) -> None:
        assert check is True
        calls.append(command)

    import subprocess

    monkeypatch.setattr(subprocess, "run", fake_run)
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
        "-o", str(tmp_path / "output.tif"),
        "-regularization", "12.0",
        "-weighting", "0.6",
        "-method", "HV",
        "-padding", "false",
        "-niter", "150",
        "-psf", str(psf),
    ]]


def test_median_4d_command(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def fake_run(command: list[str], *, check: bool) -> None:
        assert check is True
        calls.append(command)

    import subprocess

    monkeypatch.setattr(subprocess, "run", fake_run)

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
        "-o", str(tmp_path / "output.tif"),
        "-rx", "2",
        "-ry", "3",
        "-padding", "true",
        "-rz", "4",
        "-rt", "5",
    ]]


def test_cimg_denoising_command_omits_false_flags(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: list[list[str]] = []

    def fake_run(command: list[str], *, check: bool) -> None:
        assert check is True
        calls.append(command)

    import subprocess

    monkeypatch.setattr(subprocess, "run", fake_run)

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


def test_cimg_denoising_command_includes_algorithm_when_set(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: list[list[str]] = []

    def fake_run(command: list[str], *, check: bool) -> None:
        assert check is True
        calls.append(command)

    import subprocess

    monkeypatch.setattr(subprocess, "run", fake_run)

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


def test_hotspot_detection_command(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def fake_run(command: list[str], *, check: bool) -> None:
        assert check is True
        calls.append(command)

    import subprocess

    monkeypatch.setattr(subprocess, "run", fake_run)

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
        "-o", str(tmp_path / "output.tif"),
        "-m", "3",
        "-n", "5",
        "-pv", "0.2",
    ]]
