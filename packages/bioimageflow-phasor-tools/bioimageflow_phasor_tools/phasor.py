"""Composable PhasorPy file and lifetime tools."""

from pathlib import Path
from typing import Annotated, Any, Literal

from bioimageflow_core import (
    Arguments,
    Category,
    Connectable,
    EnvironmentSpec,
    GUIMeta,
    ImageSpec,
    IOModel,
    Layout,
    ProcessingTool,
    RowConsumption,
    Semantic,
    Template,
)


phasorpy_env = EnvironmentSpec(
    name="spectral-phasorpy",
    dependencies={
        "python": "3.12",
        "pip": [
            "numpy==2.4.2",
            "phasorpy==0.12",
            "tifffile==2026.3.3",
        ],
    },
)


class _PhasorTool(ProcessingTool):
    category = Category.SPECTRAL_ANALYSIS
    environment = phasorpy_env
    row_consumption = RowConsumption.MAPPED


class PtuToPhasor(_PhasorTool):
    """Convert a PicoQuant PTU histogram to Phasor OME-TIFF."""

    row_consumption = RowConsumption.MAPPED
    display_name = "PTU to Phasor"
    documentation = "Read a PTU TCSPC histogram and write selected phasor coordinates."
    tags = ["phasor", "flim", "ptu", "tcspc", "conversion"]

    class Inputs(IOModel):
        ptu_file: Annotated[
            Path,
            GUIMeta(display_name="PTU file", connectable=Connectable.BY_DEFAULT),
        ]
        channel: Annotated[int, GUIMeta(display_name="Channel", min=0)] = 0
        frame: Annotated[int, GUIMeta(display_name="Frame", min=0)] = 0
        dtime: Annotated[int, GUIMeta(display_name="Detector time", min=0)] = 0
        harmonic: Annotated[int, GUIMeta(display_name="Harmonic", min=1)] = 1

    class Outputs(IOModel):
        phasor_ome_tiff: Annotated[
            Path,
            GUIMeta(display_name="Phasor OME-TIFF"),
        ] = Template("{ptu_file.stem}_phasor.ome.tif")
        frequency_mhz: Annotated[float, GUIMeta(display_name="Frequency (MHz)")]
        harmonic: Annotated[int, GUIMeta(display_name="Harmonic")]

    def process_row(self, arguments: Arguments, *, context: Any = None) -> Any:
        from phasorpy.io import signal_from_ptu  # type: ignore

        source = _existing_file(arguments.ptu_file, "ptu_file")
        harmonic = _positive_int(arguments.harmonic, "harmonic")
        signal = signal_from_ptu(
            source,
            channel=_nonnegative_int(arguments.channel, "channel"),
            frame=_nonnegative_int(arguments.frame, "frame"),
            dtime=_nonnegative_int(arguments.dtime, "dtime"),
        )
        output = Path(arguments.phasor_ome_tiff)
        frequency = _signal_to_ometiff(
            signal,
            output,
            harmonic,
            f"Phasor coordinates converted from PTU file {source.name}.",
        )
        return self.Outputs(
            phasor_ome_tiff=output,
            frequency_mhz=frequency,
            harmonic=harmonic,
        )


class SdtToPhasor(_PhasorTool):
    """Convert a Becker & Hickl SDT histogram to Phasor OME-TIFF."""

    row_consumption = RowConsumption.MAPPED
    display_name = "SDT to Phasor"
    documentation = "Read one SDT dataset and write selected phasor coordinates."
    tags = ["phasor", "flim", "sdt", "tcspc", "conversion"]

    class Inputs(IOModel):
        sdt_file: Annotated[
            Path,
            GUIMeta(display_name="SDT file", connectable=Connectable.BY_DEFAULT),
        ]
        index: Annotated[int, GUIMeta(display_name="Dataset index", min=0)] = 0
        harmonic: Annotated[int, GUIMeta(display_name="Harmonic", min=1)] = 1

    class Outputs(IOModel):
        phasor_ome_tiff: Annotated[
            Path,
            GUIMeta(display_name="Phasor OME-TIFF"),
        ] = Template("{sdt_file.stem}_phasor.ome.tif")
        frequency_mhz: Annotated[float, GUIMeta(display_name="Frequency (MHz)")]
        harmonic: Annotated[int, GUIMeta(display_name="Harmonic")]

    def process_row(self, arguments: Arguments, *, context: Any = None) -> Any:
        from phasorpy.io import signal_from_sdt  # type: ignore

        source = _existing_file(arguments.sdt_file, "sdt_file")
        harmonic = _positive_int(arguments.harmonic, "harmonic")
        signal = signal_from_sdt(
            source,
            index=_nonnegative_int(arguments.index, "index"),
        )
        output = Path(arguments.phasor_ome_tiff)
        frequency = _signal_to_ometiff(
            signal,
            output,
            harmonic,
            f"Phasor coordinates converted from SDT file {source.name}.",
        )
        return self.Outputs(
            phasor_ome_tiff=output,
            frequency_mhz=frequency,
            harmonic=harmonic,
        )


class CalibratePhasor(_PhasorTool):
    """Calibrate phasor coordinates using a reference of known lifetime."""

    row_consumption = RowConsumption.MAPPED
    display_name = "Calibrate Phasor"
    documentation = "Calibrate sample phasors against a reference acquired with matching settings."
    tags = ["phasor", "flim", "calibration", "lifetime"]

    class Inputs(IOModel):
        phasor_ome_tiff: Annotated[
            Path,
            GUIMeta(display_name="Sample phasor", connectable=Connectable.BY_DEFAULT),
        ]
        reference_ome_tiff: Annotated[
            Path,
            GUIMeta(display_name="Reference phasor", connectable=Connectable.BY_DEFAULT),
        ]
        reference_lifetime_ns: Annotated[
            float,
            GUIMeta(display_name="Reference lifetime (ns)", min=0.000001),
        ]
        reference_center_method: Annotated[
            Literal["mean", "median"],
            GUIMeta(display_name="Reference center method"),
        ] = "mean"

    class Outputs(IOModel):
        calibrated_ome_tiff: Annotated[
            Path,
            GUIMeta(display_name="Calibrated phasor"),
        ] = Template("{phasor_ome_tiff.stem}_calibrated.ome.tif")
        frequency_mhz: Annotated[float, GUIMeta(display_name="Frequency (MHz)")]
        harmonic: Annotated[int, GUIMeta(display_name="Harmonic")]

    def process_row(self, arguments: Arguments, *, context: Any = None) -> Any:
        from phasorpy.lifetime import phasor_calibrate  # type: ignore

        mean, real, imag, attrs = _read_phasor(arguments.phasor_ome_tiff)
        ref_mean, ref_real, ref_imag, ref_attrs = _read_phasor(
            arguments.reference_ome_tiff
        )
        frequency, harmonic = _matching_metadata(attrs, ref_attrs)
        lifetime = _positive_float(
            arguments.reference_lifetime_ns,
            "reference_lifetime_ns",
        )
        center_method = str(arguments.reference_center_method)
        if center_method not in {"mean", "median"}:
            raise ValueError("reference_center_method must be 'mean' or 'median'.")
        calibrated_real, calibrated_imag = phasor_calibrate(
            real,
            imag,
            ref_mean,
            ref_real,
            ref_imag,
            frequency=frequency,
            lifetime=lifetime,
            method=center_method,
        )
        output = Path(arguments.calibrated_ome_tiff)
        _write_phasor(
            output,
            mean,
            calibrated_real,
            calibrated_imag,
            frequency,
            harmonic,
            _append_description(
                attrs,
                f"Calibrated with {Path(arguments.reference_ome_tiff).name} at {lifetime:g} ns using the {center_method} reference center.",
            ),
        )
        return self.Outputs(
            calibrated_ome_tiff=output,
            frequency_mhz=frequency,
            harmonic=harmonic,
        )


class FilterPhasor(_PhasorTool):
    """Median-filter and intensity-threshold phasor coordinates."""

    row_consumption = RowConsumption.MAPPED
    display_name = "Filter Phasor"
    documentation = "Apply spatial median filtering followed by mean-intensity thresholding."
    tags = ["phasor", "flim", "filter", "threshold"]

    class Inputs(IOModel):
        phasor_ome_tiff: Annotated[
            Path,
            GUIMeta(display_name="Phasor OME-TIFF", connectable=Connectable.BY_DEFAULT),
        ]
        median_size: Annotated[int, GUIMeta(display_name="Median size", min=1)] = 3
        median_repeat: Annotated[int, GUIMeta(display_name="Median repeats", min=0)] = 2
        mean_min: Annotated[float | None, GUIMeta(display_name="Minimum mean")] = None
        mean_max: Annotated[float | None, GUIMeta(display_name="Maximum mean")] = None

    class Outputs(IOModel):
        filtered_ome_tiff: Annotated[
            Path,
            GUIMeta(display_name="Filtered phasor"),
        ] = Template("{phasor_ome_tiff.stem}_filtered.ome.tif")
        valid_pixel_count: Annotated[int, GUIMeta(display_name="Valid pixels")]

    def process_row(self, arguments: Arguments, *, context: Any = None) -> Any:
        import numpy as np
        from phasorpy.filter import phasor_filter_median, phasor_threshold  # type: ignore

        mean, real, imag, attrs = _read_phasor(arguments.phasor_ome_tiff)
        frequency, harmonic = _metadata(attrs)
        size = _positive_int(arguments.median_size, "median_size")
        repeat = _nonnegative_int(arguments.median_repeat, "median_repeat")
        mean_min = _optional_finite(arguments.mean_min, "mean_min")
        mean_max = _optional_finite(arguments.mean_max, "mean_max")
        if mean_min is not None and mean_max is not None and mean_min > mean_max:
            raise ValueError("mean_min must be less than or equal to mean_max.")
        filtered = phasor_filter_median(mean, real, imag, size=size, repeat=repeat)
        filtered = phasor_threshold(
            *filtered,
            mean_min=mean_min,
            mean_max=mean_max,
        )
        output = Path(arguments.filtered_ome_tiff)
        _write_phasor(
            output,
            filtered[0],
            filtered[1],
            filtered[2],
            frequency,
            harmonic,
            _append_description(
                attrs,
                f"Median-filtered size={size}, repeat={repeat}; mean range={mean_min},{mean_max}.",
            ),
        )
        valid = np.isfinite(filtered[0]) & np.isfinite(filtered[1]) & np.isfinite(filtered[2])
        return self.Outputs(filtered_ome_tiff=output, valid_pixel_count=int(valid.sum()))


class PhasorToApparentLifetime(_PhasorTool):
    """Convert calibrated phasors to phase and modulation lifetime images."""

    row_consumption = RowConsumption.MAPPED
    display_name = "Phasor to Apparent Lifetime"
    documentation = "Calculate apparent phase and modulation lifetimes in nanoseconds."
    tags = ["phasor", "flim", "lifetime", "conversion"]

    class Inputs(IOModel):
        phasor_ome_tiff: Annotated[
            Path,
            GUIMeta(display_name="Phasor OME-TIFF", connectable=Connectable.BY_DEFAULT),
        ]

    class Outputs(IOModel):
        phase_lifetime: Annotated[
            Path,
            ImageSpec(semantics={Semantic.FEATURE}, layouts={Layout.PLANAR}),
            GUIMeta(display_name="Phase lifetime (ns)"),
        ] = Template("{phasor_ome_tiff.stem}_phase_lifetime_ns.tif")
        modulation_lifetime: Annotated[
            Path,
            ImageSpec(semantics={Semantic.FEATURE}, layouts={Layout.PLANAR}),
            GUIMeta(display_name="Modulation lifetime (ns)"),
        ] = Template("{phasor_ome_tiff.stem}_modulation_lifetime_ns.tif")
        frequency_mhz: Annotated[float, GUIMeta(display_name="Frequency (MHz)")]

    def process_row(self, arguments: Arguments, *, context: Any = None) -> Any:
        import numpy as np
        import tifffile
        from phasorpy.lifetime import phasor_to_apparent_lifetime  # type: ignore

        _, real, imag, attrs = _read_phasor(arguments.phasor_ome_tiff)
        frequency, _ = _metadata(attrs)
        with np.errstate(divide="ignore", invalid="ignore"):
            phase, modulation = phasor_to_apparent_lifetime(
                real,
                imag,
                frequency=frequency,
            )
        phase = np.asarray(phase, dtype=np.float32)
        modulation = np.asarray(modulation, dtype=np.float32)
        phase[~np.isfinite(phase)] = np.nan
        modulation[~np.isfinite(modulation)] = np.nan
        phase_path = Path(arguments.phase_lifetime)
        modulation_path = Path(arguments.modulation_lifetime)
        phase_path.parent.mkdir(parents=True, exist_ok=True)
        modulation_path.parent.mkdir(parents=True, exist_ok=True)
        tifffile.imwrite(phase_path, phase)
        tifffile.imwrite(modulation_path, modulation)
        return self.Outputs(
            phase_lifetime=phase_path,
            modulation_lifetime=modulation_path,
            frequency_mhz=frequency,
        )


def _signal_to_ometiff(
    signal: Any,
    output: Path,
    harmonic: int,
    description: str,
) -> float:
    from phasorpy.phasor import phasor_from_signal  # type: ignore

    frequency = _positive_float(signal.attrs.get("frequency"), "signal frequency")
    mean, real, imag = phasor_from_signal(signal, harmonic=harmonic)
    _write_phasor(output, mean, real, imag, frequency, harmonic, description)
    return frequency


def _read_phasor(path: Path | str) -> tuple[Any, Any, Any, dict[str, Any]]:
    from phasorpy.io import phasor_from_ometiff  # type: ignore

    source = _existing_file(path, "phasor_ome_tiff")
    mean, real, imag, attrs = phasor_from_ometiff(source)
    _metadata(attrs)
    return mean, real, imag, attrs


def _write_phasor(
    path: Path,
    mean: Any,
    real: Any,
    imag: Any,
    frequency: float,
    harmonic: int,
    description: str,
) -> None:
    from phasorpy.io import phasor_to_ometiff  # type: ignore

    path.parent.mkdir(parents=True, exist_ok=True)
    phasor_to_ometiff(
        path,
        mean,
        real,
        imag,
        frequency=frequency,
        harmonic=harmonic,
        description=description,
    )


def _metadata(attrs: dict[str, Any]) -> tuple[float, int]:
    frequency = _positive_float(attrs.get("frequency"), "phasor frequency")
    harmonic_value = attrs.get("harmonic", 1)
    if hasattr(harmonic_value, "item"):
        harmonic_value = harmonic_value.item()
    harmonic = _positive_int(harmonic_value, "phasor harmonic")
    return frequency, harmonic


def _matching_metadata(
    sample: dict[str, Any], reference: dict[str, Any]
) -> tuple[float, int]:
    import math

    frequency, harmonic = _metadata(sample)
    ref_frequency, ref_harmonic = _metadata(reference)
    if not math.isclose(frequency, ref_frequency, rel_tol=1e-9, abs_tol=1e-12):
        raise ValueError("Sample and reference phasors must use the same frequency.")
    if harmonic != ref_harmonic:
        raise ValueError("Sample and reference phasors must use the same harmonic.")
    return frequency, harmonic


def _append_description(attrs: dict[str, Any], operation: str) -> str:
    value = attrs.get("description")
    previous = str(value).strip() if value is not None else ""
    return f"{previous} {operation}".strip()


def _existing_file(value: Path | str, name: str) -> Path:
    path = Path(value)
    if not path.is_file():
        raise ValueError(f"{name} must be an existing file: {path}")
    return path


def _positive_int(value: Any, name: str) -> int:
    import numbers

    if isinstance(value, bool) or not isinstance(value, numbers.Integral) or int(value) < 1:
        raise ValueError(f"{name} must be a positive integer.")
    return int(value)


def _nonnegative_int(value: Any, name: str) -> int:
    import numbers

    if isinstance(value, bool) or not isinstance(value, numbers.Integral) or int(value) < 0:
        raise ValueError(f"{name} must be a non-negative integer.")
    return int(value)


def _positive_float(value: Any, name: str) -> float:
    import math

    try:
        result = float(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{name} must be a finite value greater than zero.") from error
    if not math.isfinite(result) or result <= 0:
        raise ValueError(f"{name} must be a finite value greater than zero.")
    return result


def _optional_finite(value: Any, name: str) -> float | None:
    import math

    if value is None:
        return None
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{name} must be finite when provided.")
    return result
