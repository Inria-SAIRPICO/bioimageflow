"""Shared SAIRPICO tool definitions that must stay Python 3.9-compatible."""

from __future__ import annotations

import csv
import math
from numbers import Integral
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, Optional

if TYPE_CHECKING:
    from typing import TypeAlias

from bioimageflow_core import (
    Connectable,
    EnvironmentSpec,
    GUIMeta,
    ImageSpec,
    Layout,
    Semantic,
    run_external_command_with_staged_output,
)


LEGACY_IMAGE_FORMATS = {"png", "tiff", "tif"}

simglib_env = EnvironmentSpec(
    name="simglib",
    dependencies={
        "python": "3.9",
        "conda": ["bioimageit::simglib==0.1.2"],
        "channels": ["conda-forge", "bioimageit"],
    },
)

cimgdenoising_env = EnvironmentSpec(
    name="cimgdenoising",
    dependencies={
        "python": "3.9",
        "conda": ["bioimageit::cimgdenoising==1.0.0"],
        "channels": ["conda-forge", "bioimageit"],
    },
)

hotspot_env = EnvironmentSpec(
    name="hotspot",
    dependencies={
        "python": "3.9",
        "conda": [
            "bioimageit::hotspot==1.0.0",
            "imageio==2.37.0",
            "numpy==1.26.4",
            "scipy==1.13.1",
            "tifffile==2024.2.12",
        ],
        "channels": ["conda-forge", "bioimageit"],
    },
)

SAIRPICO_BINARIES = (
    "simggaussian3dpsf",
    "simggibsonlannipsf",
    "simgrichardsonlucy2d",
    "simgrichardsonlucy2dslice",
    "simgrichardsonlucy3d",
    "simgwiener2d",
    "simgwiener2dslice",
    "simgwiener3d",
    "simgspitfiredeconv2d",
    "simgspitfiredeconv2dslice",
    "simgspitfiredeconv3d",
    "simgmedian2d",
    "simgmedian3d",
    "simgmedian4d",
    "denoise",
    "hotSpotDetection",
)

RICHARDSON_LUCY_BINARIES = {
    "2D": "simgrichardsonlucy2d",
    "2D Slice": "simgrichardsonlucy2dslice",
    "3D": "simgrichardsonlucy3d",
}

WIENER_BINARIES = {
    "2D": "simgwiener2d",
    "2D Slice": "simgwiener2dslice",
    "3D": "simgwiener3d",
}

SPITFIRE_BINARIES = {
    "2D": "simgspitfiredeconv2d",
    "2D Slice": "simgspitfiredeconv2dslice",
    "3D": "simgspitfiredeconv3d",
}

MEDIAN_BINARIES = {
    "2D": "simgmedian2d",
    "3D": "simgmedian3d",
    "4D": "simgmedian4d",
}


IntensityImage: TypeAlias = Annotated[
    Path,
    ImageSpec(
        semantics={Semantic.INTENSITY},
        layouts={
            Layout.PLANAR,
            Layout.PLANAR_TIME,
            Layout.VOLUMETRIC,
            Layout.VOLUMETRIC_TIME,
        },
        formats=LEGACY_IMAGE_FORMATS,
    ),
    GUIMeta(
        display_name="Input image",
        description="Intensity TIFF image to process.",
        connectable=Connectable.BY_DEFAULT,
    ),
]

OptionalPsfImage: TypeAlias = Annotated[
    Optional[Path],
    ImageSpec(
        semantics={Semantic.INTENSITY},
        layouts={Layout.VOLUMETRIC},
        formats=LEGACY_IMAGE_FORMATS,
    ),
    GUIMeta(
        display_name="PSF image",
        description="3D point-spread-function image used for 3D deconvolution.",
        connectable=Connectable.NOT_BY_DEFAULT,
        group="psf",
    ),
]


def _run_with_staged_output(command: list[Any], output_path: Path) -> None:
    context = str(command[0]) if command else "SAIRPICO command"
    run_external_command_with_staged_output(
        command,
        output_path=output_path,
        context=context,
    )


def _bool(value: bool) -> str:
    if not isinstance(value, bool):
        raise ValueError("Boolean command parameters must be true or false.")
    return "true" if value else "false"


def _ensure_output_parent(path: Path) -> Path:
    output_path = Path(path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    return output_path


def _parse_csv_list(value: Any) -> list[str]:
    if value is None:
        return list(SAIRPICO_BINARIES)
    if isinstance(value, (list, tuple)):
        return [str(item).strip() for item in value if str(item).strip()]
    return [item.strip() for item in str(value).split(",") if item.strip()]


def _require_choice(value: Any, name: str, choices: tuple[str, ...]) -> str:
    if not isinstance(value, str) or value not in choices:
        expected = ", ".join(repr(choice) for choice in choices)
        raise ValueError(f"{name} must be one of: {expected}.")
    return value


def _require_finite(
    value: Any,
    name: str,
    *,
    minimum: Optional[float] = None,
    maximum: Optional[float] = None,
    strictly_positive: bool = False,
) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a finite number.")
    try:
        number = float(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{name} must be a finite number.") from error
    if not math.isfinite(number):
        raise ValueError(f"{name} must be a finite number.")
    if strictly_positive and number <= 0:
        raise ValueError(f"{name} must be greater than zero.")
    if minimum is not None and number < minimum:
        raise ValueError(f"{name} must be greater than or equal to {minimum}.")
    if maximum is not None and number > maximum:
        raise ValueError(f"{name} must be less than or equal to {maximum}.")
    return number


def _require_integer(value: Any, name: str, *, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, Integral):
        raise ValueError(f"{name} must be an integer.")
    number = int(value)
    if number < minimum:
        raise ValueError(f"{name} must be greater than or equal to {minimum}.")
    return number


def _require_psf(psf_image: Optional[Path]) -> Path:
    psf_path = Path(psf_image) if psf_image is not None else None
    if psf_path is None or not psf_path.is_file():
        raise FileNotFoundError(
            f"psf_image must point to an existing PSF image for 3D deconvolution: "
            f"{psf_image}"
        )
    return psf_path


def _write_sairpico_environment_report(
    *,
    binaries: Any = None,
    report_csv: Path = Path("sairpico_environment.csv"),
) -> dict[str, Any]:
    """Write a SAIRPICO binary availability report for package diagnostics."""
    import shutil

    rows = []
    for binary in _parse_csv_list(binaries):
        path = shutil.which(binary)
        rows.append(
            {
                "binary": binary,
                "available": bool(path),
                "path": path or "",
            }
        )

    output = _ensure_output_parent(report_csv)
    with output.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["binary", "available", "path"])
        writer.writeheader()
        writer.writerows(rows)
    available_count = sum(1 for row in rows if row["available"])
    missing_count = len(rows) - available_count
    return {
        "report_csv": output,
        "available_count": available_count,
        "missing_count": missing_count,
        "ready": missing_count == 0,
    }


def _write_sairpico_version_report(
    *,
    binaries: Any = None,
    version_argument: str = "--version",
    timeout_seconds: float = 5.0,
    report_csv: Path = Path("sairpico_versions.csv"),
) -> dict[str, Any]:
    """Write a SAIRPICO CLI version report for package diagnostics."""
    import subprocess

    rows = []
    for binary in _parse_csv_list(binaries):
        command = [binary]
        if version_argument:
            command.append(str(version_argument))
        try:
            completed = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                timeout=float(timeout_seconds),
            )
            output = (completed.stdout or completed.stderr or "").strip()
            rows.append(
                {
                    "binary": binary,
                    "returncode": int(completed.returncode),
                    "version": output.splitlines()[0] if output else "",
                    "error": "",
                }
            )
        except Exception as exc:
            rows.append(
                {
                    "binary": binary,
                    "returncode": -1,
                    "version": "",
                    "error": str(exc),
                }
            )

    output = _ensure_output_parent(report_csv)
    fieldnames = ["binary", "returncode", "version", "error"]
    with output.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    reported_count = sum(1 for row in rows if row["returncode"] == 0 and row["version"])
    return {
        "report_csv": output,
        "reported_count": reported_count,
        "failed_count": len(rows) - reported_count,
    }
