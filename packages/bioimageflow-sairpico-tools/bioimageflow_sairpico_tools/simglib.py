"""SAIRPICO tools that execute in the Python 3.9 simglib environment."""

from pathlib import Path
from typing import Annotated, Any, Literal

from bioimageflow_core import (
    Arguments,
    Category,
    GUIMeta,
    ImageSpec,
    IOModel,
    Layout,
    ProcessingTool,
    RowConsumption,
    Semantic,
    Template,
)

from ._common import (
    IntensityImage,
    MEDIAN_BINARIES,
    OptionalPsfImage,
    RICHARDSON_LUCY_BINARIES,
    SPITFIRE_BINARIES,
    WIENER_BINARIES,
    _bool,
    _ensure_output_parent,
    _require_choice,
    _require_finite,
    _require_integer,
    _require_psf,
    _run_with_staged_output,
    simglib_env,
)


class GaussianPSF(ProcessingTool):
    """Generate a 3D Gaussian point-spread function with simglib."""

    row_consumption = RowConsumption.MAPPED
    display_name = "Gaussian PSF"
    documentation = "Generate a 3D Gaussian point-spread function using simglib."
    category = Category.RESTORATION
    tags = ["sairpico", "simglib", "psf"]
    environment = simglib_env

    class Inputs(IOModel):
        width: Annotated[
            int, GUIMeta("Width", "Image width in pixels.", min=1, step=1)
        ] = 256
        height: Annotated[
            int, GUIMeta("Height", "Image height in pixels.", min=1, step=1)
        ] = 256
        depth: Annotated[
            int, GUIMeta("Depth", "Image depth in planes.", min=1, step=1)
        ] = 20
        sigmaxy: Annotated[
            float, GUIMeta("Sigma XY", "Gaussian sigma in XY.", min=0.0)
        ] = 1.0
        sigmaz: Annotated[
            float, GUIMeta("Sigma Z", "Gaussian sigma in Z.", min=0.0)
        ] = 1.0

    class Outputs(IOModel):
        output_image: Annotated[
            Path,
            ImageSpec(
                semantics={Semantic.INTENSITY},
                layouts={Layout.VOLUMETRIC},
                formats={"tiff"},
            ),
            GUIMeta("Output PSF", "Generated 3D Gaussian PSF image."),
        ] = Template("gaussian_psf.tif")

    def process_row(self, arguments: Arguments, *, context: Any = None) -> Any:
        width = _require_integer(arguments.width, "width", minimum=1)
        height = _require_integer(arguments.height, "height", minimum=1)
        depth = _require_integer(arguments.depth, "depth", minimum=1)
        sigmaxy = _require_finite(arguments.sigmaxy, "sigmaxy", strictly_positive=True)
        sigmaz = _require_finite(arguments.sigmaz, "sigmaz", strictly_positive=True)
        output_path = _ensure_output_parent(arguments.output_image)
        _run_with_staged_output(
            [
                "simggaussian3dpsf",
                "-o",
                output_path,
                "-sigmaxy",
                sigmaxy,
                "-sigmaz",
                sigmaz,
                "-depth",
                depth,
                "-height",
                height,
                "-width",
                width,
            ],
            output_path,
        )
        return self.Outputs(output_image=output_path)


class GibsonLanniPSF(ProcessingTool):
    """Generate a 3D Gibson-Lanni point-spread function with simglib."""

    row_consumption = RowConsumption.MAPPED
    display_name = "Gibson-Lanni PSF"
    documentation = "Generate a 3D Gibson-Lanni point-spread function using simglib."
    category = Category.RESTORATION
    tags = ["sairpico", "simglib", "psf"]
    environment = simglib_env

    class Inputs(IOModel):
        width: Annotated[
            int, GUIMeta("Width", "Image width in pixels.", min=1, step=1)
        ] = 256
        height: Annotated[
            int, GUIMeta("Height", "Image height in pixels.", min=1, step=1)
        ] = 256
        depth: Annotated[
            int, GUIMeta("Depth", "Image depth in planes.", min=1, step=1)
        ] = 20
        wavelength: Annotated[
            float, GUIMeta("Wavelength", "Excitation wavelength in nm.", min=0.0)
        ] = 610.0
        psxy: Annotated[
            float, GUIMeta("Pixel size XY", "Pixel size in XY in nm.", min=0.0)
        ] = 100.0
        psz: Annotated[
            float, GUIMeta("Pixel size Z", "Pixel size in Z in nm.", min=0.0)
        ] = 250.0
        na: Annotated[float, GUIMeta("NA", "Numerical aperture.", min=0.0)] = 1.4
        ni: Annotated[
            float, GUIMeta("Immersion RI", "Immersion refractive index.", min=0.0)
        ] = 1.5
        ns: Annotated[
            float, GUIMeta("Sample RI", "Sample refractive index.", min=0.0)
        ] = 1.3
        ti: Annotated[
            float,
            GUIMeta("Working distance", "Working distance in micrometers.", min=0.0),
        ] = 150.0

    class Outputs(IOModel):
        output_image: Annotated[
            Path,
            ImageSpec(
                semantics={Semantic.INTENSITY},
                layouts={Layout.VOLUMETRIC},
                formats={"tiff"},
            ),
            GUIMeta("Output PSF", "Generated Gibson-Lanni PSF image."),
        ] = Template("gibson_lanni_psf.tif")

    def process_row(self, arguments: Arguments, *, context: Any = None) -> Any:
        width = _require_integer(arguments.width, "width", minimum=1)
        height = _require_integer(arguments.height, "height", minimum=1)
        depth = _require_integer(arguments.depth, "depth", minimum=1)
        optical_parameters = {
            name: _require_finite(
                getattr(arguments, name), name, strictly_positive=True
            )
            for name in ("wavelength", "psxy", "psz", "na", "ni", "ns", "ti")
        }
        output_path = _ensure_output_parent(arguments.output_image)
        _run_with_staged_output(
            [
                "simggibsonlannipsf",
                "-o",
                output_path,
                "-width",
                width,
                "-height",
                height,
                "-depth",
                depth,
                "-wavelength",
                optical_parameters["wavelength"],
                "-psxy",
                optical_parameters["psxy"],
                "-psz",
                optical_parameters["psz"],
                "-na",
                optical_parameters["na"],
                "-ni",
                optical_parameters["ni"],
                "-ns",
                optical_parameters["ns"],
                "-ti",
                optical_parameters["ti"],
            ],
            output_path,
        )
        return self.Outputs(output_image=output_path)


class RichardsonLucyDeconvolution(ProcessingTool):
    """Run Richardson-Lucy deconvolution with simglib."""

    row_consumption = RowConsumption.MAPPED
    display_name = "Richardson-Lucy Deconvolution"
    documentation = (
        "Run 2D, 2D-slice, or 3D Richardson-Lucy deconvolution using simglib."
    )
    category = Category.DECONVOLUTION
    tags = ["sairpico", "simglib", "deconvolution"]
    environment = simglib_env

    class Inputs(IOModel):
        input_image: IntensityImage
        deconvolution_type: Annotated[
            Literal["2D", "2D Slice", "3D"],
            GUIMeta(
                "Mode", "Choose 2D, 2D Slice, or 3D deconvolution.", group="general"
            ),
        ] = "2D"
        sigma: Annotated[
            float,
            GUIMeta("Sigma", "Gaussian PSF width for 2D modes.", min=0.0, group="psf"),
        ] = 1.5
        psf_image: OptionalPsfImage = None
        niter: Annotated[
            int,
            GUIMeta(
                "Iterations", "Number of iterations.", min=1, step=1, group="advanced"
            ),
        ] = 15
        regularization_lambda: Annotated[
            float,
            GUIMeta(
                "Regularization lambda",
                "Regularization parameter for non-3D modes.",
                min=0.0,
                group="advanced",
            ),
        ] = 0.0
        padding: Annotated[
            bool,
            GUIMeta(
                "Padding", "Process border pixels using padding.", group="advanced"
            ),
        ] = False

    class Outputs(IOModel):
        output_image: Annotated[
            Path,
            ImageSpec(semantics={Semantic.INTENSITY}, formats={"tiff"}),
            GUIMeta("Deconvolved image", "Output deconvolved image."),
        ] = Template("{input_image.stem}_deconvolved.tif")

    def process_row(self, arguments: Arguments, *, context: Any = None) -> Any:
        mode = _require_choice(
            arguments.deconvolution_type,
            "deconvolution_type",
            tuple(RICHARDSON_LUCY_BINARIES),
        )
        niter = _require_integer(arguments.niter, "niter", minimum=1)
        regularization_lambda = _require_finite(
            arguments.regularization_lambda,
            "regularization_lambda",
            minimum=0.0,
        )
        padding = _bool(arguments.padding)
        psf_path = _require_psf(arguments.psf_image) if mode == "3D" else None
        sigma = (
            None
            if mode == "3D"
            else _require_finite(arguments.sigma, "sigma", strictly_positive=True)
        )
        output_path = _ensure_output_parent(arguments.output_image)
        command: list[Any] = [
            RICHARDSON_LUCY_BINARIES[mode],
            "-i",
            arguments.input_image,
            "-o",
            output_path,
            "-niter",
            niter,
            "-padding",
            padding,
        ]
        if mode == "3D":
            command += ["-psf", psf_path]
        else:
            command += ["-sigma", sigma, "-lambda", regularization_lambda]
        _run_with_staged_output(command, output_path)
        return self.Outputs(output_image=output_path)


class WienerDeconvolution(ProcessingTool):
    """Run Wiener deconvolution with simglib."""

    row_consumption = RowConsumption.MAPPED
    display_name = "Wiener Deconvolution"
    documentation = "Run 2D, 2D-slice, or 3D Wiener deconvolution using simglib."
    category = Category.DECONVOLUTION
    tags = ["sairpico", "simglib", "deconvolution"]
    environment = simglib_env

    class Inputs(IOModel):
        input_image: IntensityImage
        deconvolution_type: Annotated[
            Literal["2D", "2D Slice", "3D"],
            GUIMeta(
                "Mode", "Choose 2D, 2D Slice, or 3D deconvolution.", group="general"
            ),
        ] = "2D"
        sigma: Annotated[
            float,
            GUIMeta("Sigma", "Gaussian PSF width for 2D modes.", min=0.0, group="psf"),
        ] = 1.5
        psf_image: OptionalPsfImage = None
        regularization_lambda: Annotated[
            float,
            GUIMeta(
                "Regularization lambda",
                "Regularization parameter passed as -lambda.",
                min=0.0,
                group="advanced",
            ),
        ] = 0.01
        padding: Annotated[
            bool,
            GUIMeta(
                "Padding", "Process border pixels using padding.", group="advanced"
            ),
        ] = False

    class Outputs(IOModel):
        output_image: Annotated[
            Path,
            ImageSpec(semantics={Semantic.INTENSITY}, formats={"tiff"}),
            GUIMeta("Deconvolved image", "Output deconvolved image."),
        ] = Template("{input_image.stem}_deconvolved.tif")

    def process_row(self, arguments: Arguments, *, context: Any = None) -> Any:
        mode = _require_choice(
            arguments.deconvolution_type,
            "deconvolution_type",
            tuple(WIENER_BINARIES),
        )
        regularization_lambda = _require_finite(
            arguments.regularization_lambda,
            "regularization_lambda",
            minimum=0.0,
        )
        padding = _bool(arguments.padding)
        psf_path = _require_psf(arguments.psf_image) if mode == "3D" else None
        sigma = (
            None
            if mode == "3D"
            else _require_finite(arguments.sigma, "sigma", strictly_positive=True)
        )
        output_path = _ensure_output_parent(arguments.output_image)
        command: list[Any] = [
            WIENER_BINARIES[mode],
            "-i",
            arguments.input_image,
            "-o",
            output_path,
            "-lambda",
            regularization_lambda,
            "-padding",
            padding,
        ]
        if mode == "3D":
            command += ["-psf", psf_path]
        else:
            command += ["-sigma", sigma]
        _run_with_staged_output(command, output_path)
        return self.Outputs(output_image=output_path)


class SpitfireDeconvolution(ProcessingTool):
    """Run SPITFIR(e) deconvolution with simglib."""

    row_consumption = RowConsumption.MAPPED
    display_name = "SPITFIR(e) Deconvolution"
    documentation = "Run 2D, 2D-slice, or 3D SPITFIR(e) deconvolution using simglib."
    category = Category.DECONVOLUTION
    tags = ["sairpico", "simglib", "deconvolution"]
    environment = simglib_env

    class Inputs(IOModel):
        input_image: IntensityImage
        deconvolution_type: Annotated[
            Literal["2D", "2D Slice", "3D"],
            GUIMeta(
                "Mode", "Choose 2D, 2D Slice, or 3D deconvolution.", group="general"
            ),
        ] = "2D"
        sigma: Annotated[
            float,
            GUIMeta("Sigma", "Gaussian PSF width for 2D modes.", min=0.0, group="psf"),
        ] = 1.5
        psf_image: OptionalPsfImage = None
        regularization: Annotated[
            float,
            GUIMeta(
                "Regularization",
                "Regularization parameter as pow(2, -x).",
                group="advanced",
            ),
        ] = 12.0
        weighting: Annotated[
            float,
            GUIMeta(
                "Weighting",
                "SPITFIR(e) weighting parameter.",
                min=0.0,
                max=1.0,
                group="advanced",
            ),
        ] = 0.6
        method: Annotated[
            Literal["HV", "SV"],
            GUIMeta("Method", "Regularization method.", group="advanced"),
        ] = "HV"
        padding: Annotated[
            bool,
            GUIMeta(
                "Padding", "Process border pixels using padding.", group="advanced"
            ),
        ] = False
        niter: Annotated[
            int,
            GUIMeta(
                "Iterations", "Number of iterations.", min=1, step=1, group="advanced"
            ),
        ] = 200

    class Outputs(IOModel):
        output_image: Annotated[
            Path,
            ImageSpec(semantics={Semantic.INTENSITY}, formats={"tiff"}),
            GUIMeta("Deconvolved image", "Output deconvolved image."),
        ] = Template("{input_image.stem}_deconvolved.tif")

    def process_row(self, arguments: Arguments, *, context: Any = None) -> Any:
        mode = _require_choice(
            arguments.deconvolution_type,
            "deconvolution_type",
            tuple(SPITFIRE_BINARIES),
        )
        regularization = _require_finite(arguments.regularization, "regularization")
        weighting = _require_finite(
            arguments.weighting, "weighting", minimum=0.0, maximum=1.0
        )
        method = _require_choice(arguments.method, "method", ("HV", "SV"))
        niter = _require_integer(arguments.niter, "niter", minimum=1)
        padding = _bool(arguments.padding)
        psf_path = _require_psf(arguments.psf_image) if mode == "3D" else None
        sigma = (
            None
            if mode == "3D"
            else _require_finite(arguments.sigma, "sigma", strictly_positive=True)
        )
        output_path = _ensure_output_parent(arguments.output_image)
        command: list[Any] = [
            SPITFIRE_BINARIES[mode],
            "-i",
            arguments.input_image,
            "-o",
            output_path,
            "-regularization",
            regularization,
            "-weighting",
            weighting,
            "-method",
            method,
            "-padding",
            padding,
            "-niter",
            niter,
        ]
        if mode == "3D":
            command += ["-psf", psf_path]
        else:
            command += ["-sigma", sigma]
        _run_with_staged_output(command, output_path)
        return self.Outputs(output_image=output_path)


class MedianDenoising(ProcessingTool):
    """Run simglib median denoising."""

    row_consumption = RowConsumption.MAPPED
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
        radius_x: Annotated[
            int, GUIMeta("Radius X", "Filter radius in X.", min=0, step=1)
        ] = 2
        radius_y: Annotated[
            int, GUIMeta("Radius Y", "Filter radius in Y.", min=0, step=1)
        ] = 2
        radius_z: Annotated[
            int,
            GUIMeta(
                "Radius Z", "Filter radius in Z for 3D and 4D modes.", min=0, step=1
            ),
        ] = 1
        radius_t: Annotated[
            int,
            GUIMeta("Radius T", "Filter radius in time for 4D mode.", min=0, step=1),
        ] = 1
        padding: Annotated[
            bool, GUIMeta("Padding", "Process border pixels using padding.")
        ] = False

    class Outputs(IOModel):
        output_image: Annotated[
            Path,
            ImageSpec(semantics={Semantic.INTENSITY}, formats={"tiff"}),
            GUIMeta("Filtered image", "Median filtered output image."),
        ] = Template("{input_image.stem}_filtered.tif")

    def process_row(self, arguments: Arguments, *, context: Any = None) -> Any:
        mode = _require_choice(
            arguments.denoising_type,
            "denoising_type",
            tuple(MEDIAN_BINARIES),
        )
        radius_x = _require_integer(arguments.radius_x, "radius_x", minimum=0)
        radius_y = _require_integer(arguments.radius_y, "radius_y", minimum=0)
        radius_z = _require_integer(arguments.radius_z, "radius_z", minimum=0)
        radius_t = _require_integer(arguments.radius_t, "radius_t", minimum=0)
        padding = _bool(arguments.padding)
        output_path = _ensure_output_parent(arguments.output_image)
        command: list[Any] = [
            MEDIAN_BINARIES[mode],
            "-i",
            arguments.input_image,
            "-o",
            output_path,
            "-rx",
            radius_x,
            "-ry",
            radius_y,
            "-padding",
            padding,
        ]
        if mode in {"3D", "4D"}:
            command += ["-rz", radius_z]
        if mode == "4D":
            command += ["-rt", radius_t]
        _run_with_staged_output(command, output_path)
        return self.Outputs(output_image=output_path)
