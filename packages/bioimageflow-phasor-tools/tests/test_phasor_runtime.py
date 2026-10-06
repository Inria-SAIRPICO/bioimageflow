"""Optional real PhasorPy runtime acceptance tests."""

import math
from pathlib import Path

import numpy as np
import pytest

from bioimageflow_core import Arguments
from bioimageflow_phasor_tools import (
    CalibratePhasor,
    FilterPhasor,
    PhasorToApparentLifetime,
)


pytestmark = [pytest.mark.package_tools, pytest.mark.complete]


def test_real_phasorpy_ometiff_filter_and_lifetime_roundtrip(tmp_path: Path) -> None:
    pytest.importorskip("phasorpy")
    from phasorpy.io import phasor_from_ometiff, phasor_to_ometiff

    source = tmp_path / "source.ome.tif"
    mean = np.full((8, 9), 10.0, dtype=np.float32)
    real = np.full((8, 9), 0.5, dtype=np.float32)
    imag = np.full((8, 9), 0.5, dtype=np.float32)
    phasor_to_ometiff(
        source,
        mean,
        real,
        imag,
        frequency=80.0,
        harmonic=1,
    )
    reference = tmp_path / "reference.ome.tif"
    phasor_to_ometiff(
        reference,
        np.full((4, 5), 10.0, dtype=np.float32),
        np.full((4, 5), 0.5, dtype=np.float32),
        np.full((4, 5), 0.5, dtype=np.float32),
        frequency=80.0,
        harmonic=1,
    )
    calibrated = tmp_path / "calibrated.ome.tif"
    CalibratePhasor().process_row(
        Arguments(
            phasor_ome_tiff=source,
            reference_ome_tiff=reference,
            reference_lifetime_ns=1.9894,
            reference_center_method="mean",
            calibrated_ome_tiff=calibrated,
        )
    )
    filtered = tmp_path / "filtered.ome.tif"
    filter_result = FilterPhasor().process_row(
        Arguments(
            phasor_ome_tiff=calibrated,
            median_size=3,
            median_repeat=1,
            mean_min=1.0,
            mean_max=None,
            filtered_ome_tiff=filtered,
        )
    )
    phase = tmp_path / "phase.tif"
    modulation = tmp_path / "modulation.tif"
    PhasorToApparentLifetime().process_row(
        Arguments(
            phasor_ome_tiff=filtered,
            phase_lifetime=phase,
            modulation_lifetime=modulation,
        )
    )

    roundtrip = phasor_from_ometiff(filtered)
    assert roundtrip[1].shape == real.shape
    assert np.isfinite(roundtrip[1]).all()
    assert filter_result.valid_pixel_count == mean.size
    import tifffile

    phase_image = tifffile.imread(phase)
    assert np.allclose(phase_image, 1.9894, rtol=0.01)


def _coordinate(tau, harmonic):
    omega_tau = 2 * math.pi * 80 * harmonic * 1e-3 * tau
    return complex(1 / (1 + omega_tau**2), omega_tau / (1 + omega_tau**2))


def _write(path, value, harmonic):
    from phasorpy.io import phasor_to_ometiff

    phasor_to_ometiff(
        path,
        np.ones((2, 3)),
        np.full((2, 3), value.real),
        np.full((2, 3), value.imag),
        frequency=80,
        harmonic=harmonic,
    )


@pytest.mark.parametrize("harmonic", [1, 2])
def test_real_lifetime_uses_selected_harmonic_without_changing_fundamental(
    tmp_path, harmonic
):
    source = tmp_path / "sample.ome.tif"
    _write(source, _coordinate(2, harmonic), harmonic)
    output = PhasorToApparentLifetime().process_row(
        Arguments(
            phasor_ome_tiff=source,
            phase_lifetime=tmp_path / "phase.tif",
            modulation_lifetime=tmp_path / "modulation.tif",
        )
    )
    assert output.frequency_mhz == 80
    import tifffile

    for path in (output.phase_lifetime, output.modulation_lifetime):
        image = tifffile.imread(path)
        assert image.dtype == np.float32
        np.testing.assert_allclose(image, 2, atol=1e-5)


def test_real_harmonic_calibration_removes_known_instrument_distortion(tmp_path):
    true = _coordinate(2, 2)
    distortion = 0.82 * complex(math.cos(0.17), math.sin(0.17))
    sample, reference = tmp_path / "sample.ome.tif", tmp_path / "reference.ome.tif"
    _write(sample, true * distortion, 2)
    _write(reference, _coordinate(1.5, 2) * distortion, 2)
    output = CalibratePhasor().process_row(
        Arguments(
            phasor_ome_tiff=sample,
            reference_ome_tiff=reference,
            reference_lifetime_ns=1.5,
            reference_center_method="mean",
            calibrated_ome_tiff=tmp_path / "calibrated.ome.tif",
        )
    )
    from phasorpy.io import phasor_from_ometiff

    _, real, imag, metadata = phasor_from_ometiff(output.calibrated_ome_tiff)
    np.testing.assert_allclose(real, true.real, atol=1e-6)
    np.testing.assert_allclose(imag, true.imag, atol=1e-6)
    assert output.frequency_mhz == metadata["frequency"] == 80
    assert output.harmonic == int(metadata["harmonic"]) == 2


def test_real_undefined_phasors_remain_nan_float32(tmp_path):
    source = tmp_path / "undefined.ome.tif"
    _write(source, complex(float("nan"), float("nan")), 2)
    output = PhasorToApparentLifetime().process_row(
        Arguments(
            phasor_ome_tiff=source,
            phase_lifetime=tmp_path / "phase.tif",
            modulation_lifetime=tmp_path / "modulation.tif",
        )
    )
    import tifffile

    for path in (output.phase_lifetime, output.modulation_lifetime):
        image = tifffile.imread(path)
        assert image.dtype == np.float32
        assert np.isnan(image).all()
