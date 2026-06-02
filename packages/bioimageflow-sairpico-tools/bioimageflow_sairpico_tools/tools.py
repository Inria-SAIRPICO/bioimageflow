"""SAIRPICO command-line wrappers."""

import csv
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
    Semantic,
    Template,
)


LEGACY_IMAGE_FORMATS = {"png", "tiff", "tif"}

simglib_env = EnvironmentSpec(
    name="simglib",
    dependencies={
        "python": "3.9",
        "conda": ["sylvainprigent::simglib=0.1.2"],
        "channels": ["conda-forge", "sylvainprigent"],
    },
)

cimgdenoising_env = EnvironmentSpec(
    name="cimgdenoising",
    dependencies={
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


IntensityImage = Annotated[
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

PsfImage = Annotated[
    Path,
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
    import subprocess

    subprocess.run([str(value) for value in command], check=True)


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


def _argument(arguments: Arguments, name: str, default: Any) -> Any:
    return getattr(arguments, name, default)


def _deconvolution_suffix(deconvolution_type: str) -> str:
    return deconvolution_type.replace(" ", "").lower()


def _require_psf(psf_image: Path | None) -> Path:
    psf_path = Path(psf_image) if psf_image is not None else None
    if psf_path is None or not psf_path.exists():
        raise FileNotFoundError(
            f"psf_image must point to an existing PSF image for 3D deconvolution: "
            f"{psf_image}"
        )
    return psf_path


class GaussianPSF(ProcessingTool):
    """Generate a 3D Gaussian point-spread function with simglib."""

    display_name = "Gaussian PSF"
    documentation = "Generate a 3D Gaussian point-spread function using simglib."
    category = Category.RESTORATION
    tags = ["sairpico", "simglib", "psf"]
    environment = simglib_env

    class Inputs(IOModel):
        width: Annotated[int, GUIMeta("Width", "Image width in pixels.", min=1, step=1)] = 256
        height: Annotated[int, GUIMeta("Height", "Image height in pixels.", min=1, step=1)] = 256
        depth: Annotated[int, GUIMeta("Depth", "Image depth in planes.", min=1, step=1)] = 20
        sigmaxy: Annotated[float, GUIMeta("Sigma XY", "Gaussian sigma in XY.", min=0.0)] = 1.0
        sigmaz: Annotated[float, GUIMeta("Sigma Z", "Gaussian sigma in Z.", min=0.0)] = 1.0

    class Outputs(IOModel):
        output_image: Annotated[
            Path,
            ImageSpec(semantics={Semantic.INTENSITY}, layouts={Layout.VOLUMETRIC}, formats={"tiff"}),
            GUIMeta("Output PSF", "Generated 3D Gaussian PSF image."),
        ] = Template("gaussian_psf.tif")

    def process_row(self, arguments: Arguments, *, context: Any = None) -> Any:
        output_path = _ensure_output_parent(arguments.output_image)
        _run([
            "simggaussian3dpsf",
            "-o", output_path,
            "-sigmaxy", arguments.sigmaxy,
            "-sigmaz", arguments.sigmaz,
            "-depth", arguments.depth,
            "-height", arguments.height,
            "-width", arguments.width,
        ])
        return self.Outputs(output_image=output_path)


class GibsonLanniPSF(ProcessingTool):
    """Generate a 3D Gibson-Lanni point-spread function with simglib."""

    display_name = "Gibson-Lanni PSF"
    documentation = "Generate a 3D Gibson-Lanni point-spread function using simglib."
    category = Category.RESTORATION
    tags = ["sairpico", "simglib", "psf"]
    environment = simglib_env

    class Inputs(IOModel):
        width: Annotated[int, GUIMeta("Width", "Image width in pixels.", min=1, step=1)] = 256
        height: Annotated[int, GUIMeta("Height", "Image height in pixels.", min=1, step=1)] = 256
        depth: Annotated[int, GUIMeta("Depth", "Image depth in planes.", min=1, step=1)] = 20
        wavelength: Annotated[float, GUIMeta("Wavelength", "Excitation wavelength in nm.", min=0.0)] = 610.0
        psxy: Annotated[float, GUIMeta("Pixel size XY", "Pixel size in XY in nm.", min=0.0)] = 100.0
        psz: Annotated[float, GUIMeta("Pixel size Z", "Pixel size in Z in nm.", min=0.0)] = 250.0
        na: Annotated[float, GUIMeta("NA", "Numerical aperture.", min=0.0)] = 1.4
        ni: Annotated[float, GUIMeta("Immersion RI", "Immersion refractive index.", min=0.0)] = 1.5
        ns: Annotated[float, GUIMeta("Sample RI", "Sample refractive index.", min=0.0)] = 1.3
        ti: Annotated[float, GUIMeta("Working distance", "Working distance in micrometers.", min=0.0)] = 150.0

    class Outputs(IOModel):
        output_image: Annotated[
            Path,
            ImageSpec(semantics={Semantic.INTENSITY}, layouts={Layout.VOLUMETRIC}, formats={"tiff"}),
            GUIMeta("Output PSF", "Generated Gibson-Lanni PSF image."),
        ] = Template("gibson_lanni_psf.tif")

    def process_row(self, arguments: Arguments, *, context: Any = None) -> Any:
        output_path = _ensure_output_parent(arguments.output_image)
        _run([
            "simggibsonlannipsf",
            "-o", output_path,
            "-width", arguments.width,
            "-height", arguments.height,
            "-depth", arguments.depth,
            "-wavelength", arguments.wavelength,
            "-psxy", arguments.psxy,
            "-psz", arguments.psz,
            "-na", arguments.na,
            "-ni", arguments.ni,
            "-ns", arguments.ns,
            "-ti", arguments.ti,
        ])
        return self.Outputs(output_image=output_path)


class RichardsonLucyDeconvolution(ProcessingTool):
    """Run Richardson-Lucy deconvolution with simglib."""

    display_name = "Richardson-Lucy Deconvolution"
    documentation = "Run 2D, 2D-slice, or 3D Richardson-Lucy deconvolution using simglib."
    category = Category.DECONVOLUTION
    tags = ["sairpico", "simglib", "deconvolution"]
    environment = simglib_env

    class Inputs(IOModel):
        input_image: IntensityImage
        deconvolution_type: Annotated[
            Literal["2D", "2D Slice", "3D"],
            GUIMeta("Mode", "Choose 2D, 2D Slice, or 3D deconvolution.", group="general"),
        ] = "2D"
        sigma: Annotated[float, GUIMeta("Sigma", "Gaussian PSF width for 2D modes.", min=0.0, group="psf")] = 1.5
        psf_image: PsfImage = None
        niter: Annotated[int, GUIMeta("Iterations", "Number of iterations.", min=1, step=1, group="advanced")] = 15
        regularization_lambda: Annotated[
            float,
            GUIMeta("Regularization lambda", "Regularization parameter for non-3D modes.", min=0.0, group="advanced"),
        ] = 0.0
        padding: Annotated[bool, GUIMeta("Padding", "Process border pixels using padding.", group="advanced")] = False

    class Outputs(IOModel):
        output_image: Annotated[
            Path,
            ImageSpec(semantics={Semantic.INTENSITY}, formats={"tiff"}),
            GUIMeta("Deconvolved image", "Output deconvolved image."),
        ] = Template("{input_image.stem}_deconvolved{ext}")

    def process_row(self, arguments: Arguments, *, context: Any = None) -> Any:
        output_path = _ensure_output_parent(arguments.output_image)
        command: list[Any] = [
            "simgrichardsonlucy" + _deconvolution_suffix(arguments.deconvolution_type),
            "-i", arguments.input_image,
            "-o", output_path,
            "-niter", arguments.niter,
            "-padding", _bool(arguments.padding),
        ]
        if arguments.deconvolution_type == "3D":
            command += ["-psf", _require_psf(arguments.psf_image)]
        else:
            command += ["-sigma", arguments.sigma, "-lambda", arguments.regularization_lambda]
        _run(command)
        return self.Outputs(output_image=output_path)


class WienerDeconvolution(ProcessingTool):
    """Run Wiener deconvolution with simglib."""

    display_name = "Wiener Deconvolution"
    documentation = "Run 2D, 2D-slice, or 3D Wiener deconvolution using simglib."
    category = Category.DECONVOLUTION
    tags = ["sairpico", "simglib", "deconvolution"]
    environment = simglib_env

    class Inputs(IOModel):
        input_image: IntensityImage
        deconvolution_type: Annotated[
            Literal["2D", "2D Slice", "3D"],
            GUIMeta("Mode", "Choose 2D, 2D Slice, or 3D deconvolution.", group="general"),
        ] = "2D"
        sigma: Annotated[float, GUIMeta("Sigma", "Gaussian PSF width for 2D modes.", min=0.0, group="psf")] = 1.5
        psf_image: PsfImage = None
        regularization_lambda: Annotated[
            float,
            GUIMeta("Regularization lambda", "Regularization parameter passed as -lambda.", min=0.0, group="advanced"),
        ] = 0.01
        padding: Annotated[bool, GUIMeta("Padding", "Process border pixels using padding.", group="advanced")] = False

    class Outputs(IOModel):
        output_image: Annotated[
            Path,
            ImageSpec(semantics={Semantic.INTENSITY}, formats={"tiff"}),
            GUIMeta("Deconvolved image", "Output deconvolved image."),
        ] = Template("{input_image.stem}_deconvolved{ext}")

    def process_row(self, arguments: Arguments, *, context: Any = None) -> Any:
        output_path = _ensure_output_parent(arguments.output_image)
        command: list[Any] = [
            "simgwiener" + _deconvolution_suffix(arguments.deconvolution_type),
            "-i", arguments.input_image,
            "-o", output_path,
            "-lambda", arguments.regularization_lambda,
            "-padding", _bool(arguments.padding),
        ]
        if arguments.deconvolution_type == "3D":
            command += ["-psf", _require_psf(arguments.psf_image)]
        else:
            command += ["-sigma", arguments.sigma]
        _run(command)
        return self.Outputs(output_image=output_path)


class SpitfireDeconvolution(ProcessingTool):
    """Run SPITFIR(e) deconvolution with simglib."""

    display_name = "SPITFIR(e) Deconvolution"
    documentation = "Run 2D, 2D-slice, or 3D SPITFIR(e) deconvolution using simglib."
    category = Category.DECONVOLUTION
    tags = ["sairpico", "simglib", "deconvolution"]
    environment = simglib_env

    class Inputs(IOModel):
        input_image: IntensityImage
        deconvolution_type: Annotated[
            Literal["2D", "2D Slice", "3D"],
            GUIMeta("Mode", "Choose 2D, 2D Slice, or 3D deconvolution.", group="general"),
        ] = "2D"
        sigma: Annotated[float, GUIMeta("Sigma", "Gaussian PSF width for 2D modes.", min=0.0, group="psf")] = 1.5
        psf_image: PsfImage = None
        regularization: Annotated[float, GUIMeta("Regularization", "Regularization parameter as pow(2, -x).", group="advanced")] = 12.0
        weighting: Annotated[float, GUIMeta("Weighting", "SPITFIR(e) weighting parameter.", min=0.0, max=1.0, group="advanced")] = 0.6
        method: Annotated[Literal["HV", "SV"], GUIMeta("Method", "Regularization method.", group="advanced")] = "HV"
        padding: Annotated[bool, GUIMeta("Padding", "Process border pixels using padding.", group="advanced")] = False
        niter: Annotated[int, GUIMeta("Iterations", "Number of iterations.", min=1, step=1, group="advanced")] = 200

    class Outputs(IOModel):
        output_image: Annotated[
            Path,
            ImageSpec(semantics={Semantic.INTENSITY}, formats={"tiff"}),
            GUIMeta("Deconvolved image", "Output deconvolved image."),
        ] = Template("{input_image.stem}_deconvolved{ext}")

    def process_row(self, arguments: Arguments, *, context: Any = None) -> Any:
        output_path = _ensure_output_parent(arguments.output_image)
        command: list[Any] = [
            "simgspitfiredeconv" + _deconvolution_suffix(arguments.deconvolution_type),
            "-i", arguments.input_image,
            "-o", output_path,
            "-regularization", arguments.regularization,
            "-weighting", arguments.weighting,
            "-method", arguments.method,
            "-padding", _bool(arguments.padding),
            "-niter", arguments.niter,
        ]
        if arguments.deconvolution_type == "3D":
            command += ["-psf", _require_psf(arguments.psf_image)]
        else:
            command += ["-sigma", arguments.sigma]
        _run(command)
        return self.Outputs(output_image=output_path)


class MedianDenoising(ProcessingTool):
    """Run simglib median denoising."""

    display_name = "Median Denoising"
    documentation = "Run 2D, 3D, or 4D median filtering using simglib."
    category = Category.RESTORATION
    tags = ["sairpico", "simglib", "denoising"]
    environment = simglib_env

    class Inputs(IOModel):
        input_image: IntensityImage
        denoising_type: Annotated[
            Literal["2D", "3D", "4D"],
            GUIMeta("Mode", "Choose 2D, 3D, or 4D median filtering.", group="general"),
        ] = "2D"
        radius_x: Annotated[int, GUIMeta("Radius X", "Filter radius in X.", min=0, step=1)] = 2
        radius_y: Annotated[int, GUIMeta("Radius Y", "Filter radius in Y.", min=0, step=1)] = 2
        radius_z: Annotated[int, GUIMeta("Radius Z", "Filter radius in Z for 3D and 4D modes.", min=0, step=1)] = 1
        radius_t: Annotated[int, GUIMeta("Radius T", "Filter radius in time for 4D mode.", min=0, step=1)] = 1
        padding: Annotated[bool, GUIMeta("Padding", "Process border pixels using padding.")] = False

    class Outputs(IOModel):
        output_image: Annotated[
            Path,
            ImageSpec(semantics={Semantic.INTENSITY}, formats={"tiff"}),
            GUIMeta("Filtered image", "Median filtered output image."),
        ] = Template("{input_image.stem}_filtered{ext}")

    def process_row(self, arguments: Arguments, *, context: Any = None) -> Any:
        output_path = _ensure_output_parent(arguments.output_image)
        command: list[Any] = [
            "simgmedian" + arguments.denoising_type.lower(),
            "-i", arguments.input_image,
            "-o", output_path,
            "-rx", arguments.radius_x,
            "-ry", arguments.radius_y,
            "-padding", _bool(arguments.padding),
        ]
        if arguments.denoising_type in {"3D", "4D"}:
            command += ["-rz", arguments.radius_z]
        if arguments.denoising_type == "4D":
            command += ["-rt", arguments.radius_t]
        _run(command)
        return self.Outputs(output_image=output_path)


class CImgDenoising(ProcessingTool):
    """Run CImg denoising algorithms."""

    display_name = "CImg Denoising"
    documentation = "Denoise 2D+T images using CImg patch, basic, and variational methods."
    category = Category.RESTORATION
    tags = ["sairpico", "cimg", "denoising"]
    environment = cimgdenoising_env

    class Inputs(IOModel):
        input_image: IntensityImage
        first: Annotated[int, GUIMeta("First frame", "First image index; 0 uses the default.", step=1)] = 0
        last: Annotated[int, GUIMeta("Last frame", "Last image index; -1 uses full depth or time.", step=1)] = -1
        alpha: Annotated[float, GUIMeta("Alpha", "Input/output alpha mixing.", min=0.0, max=1.0)] = 0.0
        scale: Annotated[float, GUIMeta("Scale", "Volume resize factor.", min=0.5, max=1.5)] = 1.0
        intensity_range: Annotated[float, GUIMeta("Range", "Automatic intensity scaling (-1) or manual scaling.")] = 1.0
        algorithm: Annotated[
            Literal["BM3D", "NLBayes", "NLMeans", "BayesNLmeans", "SAFIR", "PEWA", "OWF", "DCT", "Wiener", "Bilateral", "Gaussian", "Median", "TV", "SV", "HV"] | None,
            GUIMeta("Algorithm", "Denoising algorithm name."),
        ] = None
        gaussian_noise: Annotated[float, GUIMeta("Gaussian noise", "Add artificial Gaussian noise before denoising.", min=0.0)] = 0.0
        poisson_noise: Annotated[bool, GUIMeta("Poisson noise", "Add artificial Poisson noise before denoising.")] = False
        manual_sigma: Annotated[float, GUIMeta("Manual sigma", "Manual assumed Gaussian noise standard deviation.", min=0.0)] = 0.0
        stabilize_poisson: Annotated[bool, GUIMeta("Stabilize Poisson", "Use variance stabilization for Poisson noise removal.")] = False
        patch: Annotated[int, GUIMeta("Patch", "Patch half-size.", min=0, step=1)] = 3
        neighborhood: Annotated[int, GUIMeta("Neighborhood", "Neighborhood half-size.", min=0, step=1)] = 7
        denoising_parameter: Annotated[float, GUIMeta("Denoising parameter", "Algorithm-specific denoising parameter.")] = -1.0
        sparsity_parameter: Annotated[float, GUIMeta("Sparsity parameter", "SV/HV sparsity parameter.", min=0.1, max=0.9)] = 0.6
        iterations: Annotated[int, GUIMeta("Iterations", "Number of iterations for NDSafir.", min=1, step=1)] = 4

    class Outputs(IOModel):
        output_image: Annotated[
            Path,
            ImageSpec(semantics={Semantic.INTENSITY}, formats={"tiff"}),
            GUIMeta("Denoised image", "Output denoised image."),
        ] = Template("{input_image.stem}_denoised{ext}")

    def process_row(self, arguments: Arguments, *, context: Any = None) -> Any:
        output_path = _ensure_output_parent(arguments.output_image)
        command: list[Any] = [
            "denoise",
            "-i", arguments.input_image,
            "-o", output_path,
            "-first", arguments.first,
            "-last", arguments.last,
            "-alpha", arguments.alpha,
            "-scale", arguments.scale,
            "-range", arguments.intensity_range,
            "-ng", arguments.gaussian_noise,
            "-msg", arguments.manual_sigma,
            "-patch", arguments.patch,
            "-neigh", arguments.neighborhood,
            "-denoisep", arguments.denoising_parameter,
            "-sparsep", arguments.sparsity_parameter,
            "-iter", arguments.iterations,
        ]
        if arguments.poisson_noise:
            command.append("-np")
        if arguments.stabilize_poisson:
            command.append("-stab")
        if arguments.algorithm is not None:
            command += ["-algo", arguments.algorithm]
        _run(command)
        return self.Outputs(output_image=output_path)


class HotspotDetection(ProcessingTool):
    """Run hotspot detection."""

    display_name = "Hotspot Detection"
    documentation = "Detect hotspots in microscopy images using the hotspot command-line tool."
    category = Category.SPOT_DETECTION
    tags = ["sairpico", "hotspot", "detection"]
    environment = hotspot_env

    class Inputs(IOModel):
        input_image: IntensityImage
        patch_size: Annotated[int, GUIMeta("Patch size", "Patch radius.", min=1, step=1)] = 3
        neighborhood_size: Annotated[int, GUIMeta("Neighborhood size", "Neighborhood radius.", min=1, step=1)] = 5
        p_value: Annotated[float, GUIMeta("P-value", "False-alarm p-value.", min=0.0, max=1.0)] = 0.2

    class Outputs(IOModel):
        output_image: Annotated[
            Path,
            ImageSpec(semantics={Semantic.INTENSITY}, formats={"tiff"}),
            GUIMeta("Hotspot image", "Detected hotspots image."),
        ] = Template("{input_image.stem}_hotspot{ext}")

    def process_row(self, arguments: Arguments, *, context: Any = None) -> Any:
        output_path = _ensure_output_parent(arguments.output_image)
        _run([
            "hotSpotDetection",
            "-i", arguments.input_image,
            "-o", output_path,
            "-m", arguments.patch_size,
            "-n", arguments.neighborhood_size,
            "-pv", arguments.p_value,
        ])
        return self.Outputs(output_image=output_path)


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


class HotspotToSpots(ProcessingTool):
    """Convert thresholded hotspot images into spot coordinate tables."""

    display_name = "Hotspot To Spots"
    documentation = "Convert SAIRPICO hotspot image outputs to spot coordinate tables."
    category = Category.SPOT_DETECTION
    tags = ["sairpico", "hotspot", "spots"]
    environment = hotspot_env

    class Inputs(IOModel):
        hotspot_image: Annotated[
            Path,
            ImageSpec(semantics={Semantic.INTENSITY}, layouts={Layout.PLANAR}),
            GUIMeta("Hotspot image", "Thresholded or scored hotspot image."),
        ]
        threshold: float = 0.0

    class Outputs(IOModel):
        spots_csv: Annotated[Path, GUIMeta("Spots CSV")] = Template(
            "{hotspot_image.stem}_spots.csv"
        )
        spot_count: Annotated[int, GUIMeta("Spot count")]

    def process_row(self, arguments: Arguments, *, context: Any = None) -> Any:
        import imageio.v3 as iio
        import numpy as np

        image = iio.imread(arguments.hotspot_image).astype(np.float32)
        if image.ndim != 2:
            raise ValueError("HotspotToSpots expects a 2D hotspot image.")
        components = _hotspot_components(image > float(arguments.threshold))
        rows = []
        for spot_id, component in enumerate(components, start=1):
            yy = np.asarray([yx[0] for yx in component], dtype=np.float32)
            xx = np.asarray([yx[1] for yx in component], dtype=np.float32)
            values = image[yy.astype(int), xx.astype(int)]
            rows.append(
                {
                    "spot_id": spot_id,
                    "y": float(yy.mean()),
                    "x": float(xx.mean()),
                    "intensity": float(values.max()),
                    "score": float(values.mean()),
                    "area": int(len(component)),
                    "label": spot_id,
                }
            )

        output = _ensure_output_parent(arguments.spots_csv)
        fieldnames = ["spot_id", "y", "x", "intensity", "score", "area", "label"]
        with output.open("w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)
        return self.Outputs(spots_csv=output, spot_count=len(rows))
