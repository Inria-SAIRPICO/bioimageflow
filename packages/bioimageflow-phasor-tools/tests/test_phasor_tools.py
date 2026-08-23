"""Fast PhasorPy adapter tests."""

from pathlib import Path
import sys
import types

import numpy as np
import pytest

from bioimageflow.validation import serialize_input_schema, serialize_output_schema
from bioimageflow_core import Arguments
from bioimageflow_phasor_tools import (
    CalibratePhasor,
    FilterPhasor,
    PhasorToApparentLifetime,
    PtuToPhasor,
    SdtToPhasor,
)


pytestmark = pytest.mark.package_tools


@pytest.mark.parametrize(
    "tool",
    [PtuToPhasor, SdtToPhasor, CalibratePhasor, FilterPhasor, PhasorToApparentLifetime],
)
def test_phasor_tool_schemas_are_serializable(tool: type) -> None:
    assert serialize_input_schema(tool)
    assert serialize_output_schema(tool)


def test_ptu_conversion_uses_signal_metadata_and_writes_exchange_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Signal:
        attrs = {"frequency": 80.0}

    io_module = types.ModuleType("phasorpy.io")
    io_module.signal_from_ptu = lambda *args, **kwargs: Signal()  # type: ignore[attr-defined]

    def write(path: Path, *args: object, **kwargs: object) -> None:
        Path(path).write_bytes(b"ome")

    io_module.phasor_to_ometiff = write  # type: ignore[attr-defined]
    phasor_module = types.ModuleType("phasorpy.phasor")
    phasor_module.phasor_from_signal = lambda signal, harmonic: (  # type: ignore[attr-defined]
        np.ones((2, 3)),
        np.full((2, 3), 0.5),
        np.full((2, 3), 0.5),
    )
    monkeypatch.setitem(sys.modules, "phasorpy", types.ModuleType("phasorpy"))
    monkeypatch.setitem(sys.modules, "phasorpy.io", io_module)
    monkeypatch.setitem(sys.modules, "phasorpy.phasor", phasor_module)
    source = tmp_path / "sample.ptu"
    source.write_bytes(b"ptu")
    output = tmp_path / "sample.ome.tif"

    result = PtuToPhasor().process_row(
        Arguments(
            ptu_file=source,
            channel=0,
            frame=0,
            dtime=0,
            harmonic=1,
            phasor_ome_tiff=output,
        )
    )

    assert output.read_bytes() == b"ome"
    assert result.frequency_mhz == 80.0
    assert result.harmonic == 1


def test_filter_preserves_processing_description(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    written: dict[str, object] = {}
    arrays = (
        np.ones((2, 2)),
        np.full((2, 2), 0.5),
        np.full((2, 2), 0.5),
    )
    io_module = types.ModuleType("phasorpy.io")
    io_module.phasor_from_ometiff = lambda path: (  # type: ignore[attr-defined]
        *arrays,
        {"frequency": 80.0, "harmonic": 1, "description": "Converted from PTU."},
    )

    def write(path: Path, *values: object, **kwargs: object) -> None:
        written.update(kwargs)
        Path(path).write_bytes(b"ome")

    io_module.phasor_to_ometiff = write  # type: ignore[attr-defined]
    filter_module = types.ModuleType("phasorpy.filter")
    filter_module.phasor_filter_median = (  # type: ignore[attr-defined]
        lambda mean, real, imag, **kwargs: (mean, real, imag)
    )
    filter_module.phasor_threshold = (  # type: ignore[attr-defined]
        lambda mean, real, imag, **kwargs: (mean, real, imag)
    )
    monkeypatch.setitem(sys.modules, "phasorpy", types.ModuleType("phasorpy"))
    monkeypatch.setitem(sys.modules, "phasorpy.io", io_module)
    monkeypatch.setitem(sys.modules, "phasorpy.filter", filter_module)
    source = tmp_path / "phasor.ome.tif"
    source.write_bytes(b"ome")
    output = tmp_path / "filtered.ome.tif"

    FilterPhasor().process_row(
        Arguments(
            phasor_ome_tiff=source,
            median_size=3,
            median_repeat=1,
            mean_min=None,
            mean_max=None,
            filtered_ome_tiff=output,
        )
    )

    assert written["description"] == (
        "Converted from PTU. Median-filtered size=3, repeat=1; mean range=None,None."
    )


def test_calibration_accepts_different_reference_shape_and_selects_center_method(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: dict[str, object] = {}
    sample = tmp_path / "sample.ome.tif"
    reference = tmp_path / "reference.ome.tif"
    sample.write_bytes(b"sample")
    reference.write_bytes(b"reference")
    io_module = types.ModuleType("phasorpy.io")

    def read(path: Path):
        shape = (2, 2) if Path(path) == sample else (3, 4)
        return (
            np.ones(shape),
            np.full(shape, 0.5),
            np.full(shape, 0.5),
            {"frequency": 80.0, "harmonic": 1},
        )

    io_module.phasor_from_ometiff = read  # type: ignore[attr-defined]

    def write(path: Path, *values: object, **kwargs: object) -> None:
        Path(path).write_bytes(b"ome")

    io_module.phasor_to_ometiff = write  # type: ignore[attr-defined]
    lifetime_module = types.ModuleType("phasorpy.lifetime")

    def calibrate(
        real: np.ndarray,
        imag: np.ndarray,
        ref_mean: np.ndarray,
        ref_real: np.ndarray,
        ref_imag: np.ndarray,
        **kwargs: object,
    ) -> tuple[np.ndarray, np.ndarray]:
        calls["reference_shape"] = ref_mean.shape
        calls["method"] = kwargs["method"]
        return real, imag

    lifetime_module.phasor_calibrate = calibrate  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "phasorpy", types.ModuleType("phasorpy"))
    monkeypatch.setitem(sys.modules, "phasorpy.io", io_module)
    monkeypatch.setitem(sys.modules, "phasorpy.lifetime", lifetime_module)

    CalibratePhasor().process_row(
        Arguments(
            phasor_ome_tiff=sample,
            reference_ome_tiff=reference,
            reference_lifetime_ns=4.2,
            reference_center_method="median",
            calibrated_ome_tiff=tmp_path / "calibrated.ome.tif",
        )
    )

    assert calls == {"reference_shape": (3, 4), "method": "median"}


def test_apparent_lifetime_writes_float32_and_replaces_infinity_with_nan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    io_module = types.ModuleType("phasorpy.io")
    io_module.phasor_from_ometiff = lambda path: (  # type: ignore[attr-defined]
        np.ones((2, 2)),
        np.full((2, 2), 0.5),
        np.full((2, 2), 0.5),
        {"frequency": 80.0, "harmonic": 1},
    )
    lifetime_module = types.ModuleType("phasorpy.lifetime")
    lifetime_module.phasor_to_apparent_lifetime = lambda *args, **kwargs: (  # type: ignore[attr-defined]
        np.array([[2.0, np.inf], [2.0, 2.0]]),
        np.full((2, 2), 2.0),
    )
    monkeypatch.setitem(sys.modules, "phasorpy", types.ModuleType("phasorpy"))
    monkeypatch.setitem(sys.modules, "phasorpy.io", io_module)
    monkeypatch.setitem(sys.modules, "phasorpy.lifetime", lifetime_module)
    source = tmp_path / "phasor.ome.tif"
    source.write_bytes(b"ome")
    phase = tmp_path / "phase.tif"
    modulation = tmp_path / "modulation.tif"

    result = PhasorToApparentLifetime().process_row(
        Arguments(
            phasor_ome_tiff=source,
            phase_lifetime=phase,
            modulation_lifetime=modulation,
        )
    )

    import tifffile

    phase_image = tifffile.imread(phase)
    assert phase_image.dtype == np.float32
    assert np.isnan(phase_image[0, 1])
    assert result.frequency_mhz == 80.0
