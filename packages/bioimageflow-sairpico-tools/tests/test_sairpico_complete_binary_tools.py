"""COMPLETE tests for SAIRPICO tools against real command-line binaries."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import imageio.v3 as iio
import numpy as np
import pytest

from bioimageflow import Workflow
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


pytestmark = [
    pytest.mark.package_tools,
    pytest.mark.complete,
    pytest.mark.wetlands,
]


def _write_intensity_fixture(path: Path, *, shape: tuple[int, int] = (32, 32)) -> np.ndarray:
    yy, xx = np.mgrid[: shape[0], : shape[1]]
    image = (
        400
        + 12 * xx
        + 7 * yy
        + 2500 * np.exp(-((xx - 11) ** 2 + (yy - 9) ** 2) / 10)
        + 1800 * np.exp(-((xx - 23) ** 2 + (yy - 24) ** 2) / 14)
    )
    image = np.clip(image, 0, np.iinfo(np.uint16).max).astype(np.uint16)
    iio.imwrite(path, image)
    return image


def _read_numeric_image(path: Path) -> np.ndarray:
    assert path.exists(), f"Expected output image to exist: {path}"
    assert path.stat().st_size > 0, f"Expected output image to be non-empty: {path}"
    image = np.asarray(iio.imread(path))
    assert image.size > 0
    assert np.issubdtype(image.dtype, np.number)
    assert np.isfinite(image.astype(np.float64)).all()
    return image


def _assert_planar_output(path: Path) -> None:
    image = _read_numeric_image(path)
    assert image.shape[-2:] == (32, 32)
    assert float(image.max()) >= float(image.min())


def _assert_psf_output(path: Path) -> None:
    image = _read_numeric_image(path)
    assert image.ndim == 3
    assert image.shape == (5, 17, 19)
    assert float(image.max()) > float(image.min())


def _gaussian_psf_arguments(tmp_path: Path, output_path: Path) -> Arguments:
    return Arguments(
        width=19,
        height=17,
        depth=5,
        sigmaxy=1.2,
        sigmaz=1.8,
        output_image=output_path,
    )


def _gibson_lanni_psf_arguments(tmp_path: Path, output_path: Path) -> Arguments:
    return Arguments(
        width=19,
        height=17,
        depth=5,
        wavelength=610.0,
        psxy=100.0,
        psz=250.0,
        na=1.3,
        ni=1.5,
        ns=1.33,
        ti=150.0,
        output_image=output_path,
    )


def _richardson_lucy_arguments(tmp_path: Path, output_path: Path) -> Arguments:
    input_path = tmp_path / "richardson_lucy_input.tif"
    _write_intensity_fixture(input_path)
    return Arguments(
        input_image=input_path,
        deconvolution_type="2D",
        sigma=1.4,
        psf_image=None,
        niter=2,
        regularization_lambda=0.01,
        padding=True,
        output_image=output_path,
    )


def _wiener_arguments(tmp_path: Path, output_path: Path) -> Arguments:
    input_path = tmp_path / "wiener_input.tif"
    _write_intensity_fixture(input_path)
    return Arguments(
        input_image=input_path,
        deconvolution_type="2D",
        sigma=1.4,
        psf_image=None,
        regularization_lambda=0.01,
        padding=True,
        output_image=output_path,
    )


def _spitfire_arguments(tmp_path: Path, output_path: Path) -> Arguments:
    input_path = tmp_path / "spitfire_input.tif"
    _write_intensity_fixture(input_path)
    return Arguments(
        input_image=input_path,
        deconvolution_type="2D",
        sigma=1.4,
        psf_image=None,
        regularization=10.0,
        weighting=0.6,
        method="HV",
        padding=True,
        niter=2,
        output_image=output_path,
    )


def _median_arguments(tmp_path: Path, output_path: Path) -> Arguments:
    input_path = tmp_path / "median_input.tif"
    _write_intensity_fixture(input_path)
    return Arguments(
        input_image=input_path,
        denoising_type="2D",
        radius_x=1,
        radius_y=1,
        radius_z=1,
        radius_t=1,
        padding=True,
        output_image=output_path,
    )


def _cimg_arguments(tmp_path: Path, output_path: Path) -> Arguments:
    input_path = tmp_path / "cimg_input.tif"
    _write_intensity_fixture(input_path)
    return Arguments(
        input_image=input_path,
        first=0,
        last=-1,
        alpha=0.0,
        scale=1.0,
        intensity_range=1.0,
        algorithm="Gaussian",
        gaussian_noise=0.0,
        poisson_noise=False,
        manual_sigma=0.0,
        stabilize_poisson=False,
        patch=2,
        neighborhood=4,
        denoising_parameter=-1.0,
        sparsity_parameter=0.6,
        iterations=2,
        output_image=output_path,
    )


def _hotspot_detection_arguments(tmp_path: Path, output_path: Path) -> Arguments:
    input_path = tmp_path / "hotspot_detection_input.tif"
    image = np.zeros((32, 32), dtype=np.uint16)
    image[10, 11] = 5000
    image[23, 24] = 4500
    image[4:7, 5:8] = 300
    iio.imwrite(input_path, image)
    return Arguments(
        input_image=input_path,
        patch_size=1,
        neighborhood_size=3,
        p_value=0.5,
        output_image=output_path,
    )


@dataclass(frozen=True)
class BinaryToolCase:
    id: str
    tool_cls: type[ProcessingTool]
    binaries: tuple[str, ...]
    make_arguments: Callable[[Path, Path], Arguments]
    assert_output: Callable[[Path], None]


BINARY_TOOL_CASES = (
    BinaryToolCase(
        id="gaussian-psf",
        tool_cls=GaussianPSF,
        binaries=("simggaussian3dpsf",),
        make_arguments=_gaussian_psf_arguments,
        assert_output=_assert_psf_output,
    ),
    BinaryToolCase(
        id="gibson-lanni-psf",
        tool_cls=GibsonLanniPSF,
        binaries=("simggibsonlannipsf",),
        make_arguments=_gibson_lanni_psf_arguments,
        assert_output=_assert_psf_output,
    ),
    BinaryToolCase(
        id="richardson-lucy-deconvolution",
        tool_cls=RichardsonLucyDeconvolution,
        binaries=("simgrichardsonlucy2d",),
        make_arguments=_richardson_lucy_arguments,
        assert_output=_assert_planar_output,
    ),
    BinaryToolCase(
        id="wiener-deconvolution",
        tool_cls=WienerDeconvolution,
        binaries=("simgwiener2d",),
        make_arguments=_wiener_arguments,
        assert_output=_assert_planar_output,
    ),
    BinaryToolCase(
        id="spitfire-deconvolution",
        tool_cls=SpitfireDeconvolution,
        binaries=("simgspitfiredeconv2d",),
        make_arguments=_spitfire_arguments,
        assert_output=_assert_planar_output,
    ),
    BinaryToolCase(
        id="median-denoising",
        tool_cls=MedianDenoising,
        binaries=("simgmedian2d",),
        make_arguments=_median_arguments,
        assert_output=_assert_planar_output,
    ),
    BinaryToolCase(
        id="cimg-denoising",
        tool_cls=CImgDenoising,
        binaries=("denoise",),
        make_arguments=_cimg_arguments,
        assert_output=_assert_planar_output,
    ),
    BinaryToolCase(
        id="hotspot-detection",
        tool_cls=HotspotDetection,
        binaries=("hotSpotDetection",),
        make_arguments=_hotspot_detection_arguments,
        assert_output=_assert_planar_output,
    ),
)


TEMPORARILY_UNAVAILABLE_PACKAGE_CASE_IDS = {
    "gaussian-psf",
    "gibson-lanni-psf",
    "richardson-lucy-deconvolution",
    "wiener-deconvolution",
    "spitfire-deconvolution",
    "median-denoising",
    "cimg-denoising",
}

TEMPORARILY_UNAVAILABLE_PACKAGE_REASON = (
    "Temporarily disabled while SAIRPICO conda packages are being rebuilt for "
    "this platform. Re-enable these cases when simglib, serpico-cimgdenoising, "
    "and serpico-spitfire are available by removing "
    "TEMPORARILY_UNAVAILABLE_PACKAGE_CASE_IDS and restoring direct "
    "BINARY_TOOL_CASES parametrization."
)


def _parametrize_binary_tool_case(case: BinaryToolCase) -> pytest.ParameterSet:
    if case.id in TEMPORARILY_UNAVAILABLE_PACKAGE_CASE_IDS:
        return pytest.param(
            case,
            id=case.id,
            marks=pytest.mark.skip(reason=TEMPORARILY_UNAVAILABLE_PACKAGE_REASON),
        )
    return pytest.param(case, id=case.id)


@pytest.mark.external_binary
@pytest.mark.sairpico_binary
@pytest.mark.parametrize(
    "case",
    [_parametrize_binary_tool_case(case) for case in BINARY_TOOL_CASES],
)
def test_exported_sairpico_binary_tool_executes_real_cli(
    case: BinaryToolCase,
    tmp_path: Path,
    complete_wetlands_config: dict,
) -> None:
    output_path = tmp_path / f"{case.id}.tif"
    arguments = case.make_arguments(tmp_path, output_path)
    kwargs = vars(arguments).copy()
    kwargs.pop("output_image")

    with Workflow(
        storage_path=tmp_path / "results",
        use_wetlands=True,
        wetlands_config=complete_wetlands_config,
    ) as wf:
        output_node = case.tool_cls()(
            name=case.id.replace("-", "_"),
            output_templates={"output_image": str(output_path)},
            **kwargs,
        )
        result = wf.compute(output_node)

    assert Path(result.iloc[0]["output_image"]) == output_path
    case.assert_output(output_path)
