"""SAIRPICO tools that execute in the Python 3.9 simglib environment."""

from pathlib import Path
import sys
from typing import Annotated, Any, Literal

from bioimageflow_core import (
    Arguments,
    Category,
    GUIMeta,
    ImageSpec,
    IOModel,
    Layout,
    ProcessingTool,
    Semantic,
    Template,
)

package_root = str(Path(__file__).resolve().parent.parent)
if package_root not in sys.path:
    sys.path.insert(0, package_root)

from bioimageflow_sairpico_tools._common import (  # noqa: E402
    IntensityImage,
    OptionalPsfImage,
    _bool,
    _deconvolution_suffix,
    _ensure_output_parent,
    _require_psf,
    _run_with_staged_output,
    simglib_env,
)


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
        _run_with_staged_output([
            "simggaussian3dpsf",
            "-o", output_path,
            "-sigmaxy", arguments.sigmaxy,
            "-sigmaz", arguments.sigmaz,
            "-depth", arguments.depth,
            "-height", arguments.height,
            "-width", arguments.width,
        ], output_path)
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
        _run_with_staged_output([
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
        ], output_path)
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
        psf_image: OptionalPsfImage = None
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
        _run_with_staged_output(command, output_path)
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
        psf_image: OptionalPsfImage = None
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
        _run_with_staged_output(command, output_path)
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
        psf_image: OptionalPsfImage = None
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
        _run_with_staged_output(command, output_path)
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
        _run_with_staged_output(command, output_path)
        return self.Outputs(output_image=output_path)
