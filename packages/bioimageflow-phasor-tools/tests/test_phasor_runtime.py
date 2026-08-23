"""Optional real PhasorPy runtime acceptance tests."""

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
