"""Shared SAIRPICO tool definitions that must stay Python 3.9-compatible."""

from __future__ import annotations

import csv
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
    run_external_command,
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
        "conda": ["bioimageit::hotspot==1.0.0"],
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


def _run(command: list[Any]) -> None:
    context = str(command[0]) if command else "SAIRPICO command"
    run_external_command(command, context=context)


def _run_with_staged_output(command: list[Any], output_path: Path) -> None:
    context = str(command[0]) if command else "SAIRPICO command"
    run_external_command_with_staged_output(
        command,
        output_path=output_path,
        context=context,
    )


def _bool(value: bool) -> str:
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


def _deconvolution_suffix(deconvolution_type: str) -> str:
    return deconvolution_type.replace(" ", "").lower()


def _require_psf(psf_image: Optional[Path]) -> Path:
    psf_path = Path(psf_image) if psf_image is not None else None
    if psf_path is None or not psf_path.exists():
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
    reported_count = sum(
        1 for row in rows
        if row["returncode"] == 0 and row["version"]
    )
    return {
        "report_csv": output,
        "reported_count": reported_count,
        "failed_count": len(rows) - reported_count,
    }


def _hotspot_components(mask: Any) -> list[list[tuple[int, int]]]:
    import numpy as np

    seen = np.zeros(mask.shape, dtype=bool)
    components: list[list[tuple[int, int]]] = []
    for y, x in np.argwhere(mask):
        y = int(y)
        x = int(x)
        if seen[y, x]:
            continue
        stack = [(y, x)]
        seen[y, x] = True
        component = []
        while stack:
            cy, cx = stack.pop()
            component.append((cy, cx))
            for ny in range(max(0, cy - 1), min(mask.shape[0], cy + 2)):
                for nx in range(max(0, cx - 1), min(mask.shape[1], cx + 2)):
                    if mask[ny, nx] and not seen[ny, nx]:
                        seen[ny, nx] = True
                        stack.append((ny, nx))
        components.append(component)
    return components
