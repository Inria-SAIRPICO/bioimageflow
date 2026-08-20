"""SAIRPICO CImg denoising tool."""

from pathlib import Path
from typing import Annotated, Any, Literal, Optional

from bioimageflow_core import (
    Arguments,
    Category,
    GUIMeta,
    ImageSpec,
    IOModel,
    ProcessingTool,
    RowConsumption,
    Semantic,
    Template,
)

from ._common import (
    IntensityImage,
    _ensure_output_parent,
    _require_choice,
    _require_finite,
    _require_integer,
    _run_with_staged_output,
    cimgdenoising_env,
)


_DENOISING_ALGORITHMS = (
    "BM3D",
    "NLBayes",
    "NLMeans",
    "BayesNLmeans",
    "SAFIR",
    "PEWA",
    "OWF",
    "DCT",
    "Wiener",
    "Bilateral",
    "Gaussian",
    "Median",
    "TV",
    "SV",
    "HV",
)


class CImgDenoising(ProcessingTool):
    """Run CImg denoising algorithms."""

    row_consumption = RowConsumption.MAPPED
    display_name = "CImg Denoising"
    documentation = (
        "Denoise 2D+T images using CImg patch, basic, and variational methods."
    )
    category = Category.RESTORATION
    tags = ["sairpico", "cimg", "denoising"]
    environment = cimgdenoising_env

    class Inputs(IOModel):
        input_image: IntensityImage
        first: Annotated[
            int,
            GUIMeta("First frame", "First image index; 0 uses the default.", step=1),
        ] = 0
        last: Annotated[
            int,
            GUIMeta(
                "Last frame", "Last image index; -1 uses full depth or time.", step=1
            ),
        ] = -1
        alpha: Annotated[
            float, GUIMeta("Alpha", "Input/output alpha mixing.", min=0.0, max=1.0)
        ] = 0.0
        scale: Annotated[
            float, GUIMeta("Scale", "Volume resize factor.", min=0.5, max=1.5)
        ] = 1.0
        intensity_range: Annotated[
            float,
            GUIMeta("Range", "Automatic intensity scaling (-1) or manual scaling."),
        ] = 1.0
        algorithm: Annotated[
            Optional[
                Literal[
                    "BM3D",
                    "NLBayes",
                    "NLMeans",
                    "BayesNLmeans",
                    "SAFIR",
                    "PEWA",
                    "OWF",
                    "DCT",
                    "Wiener",
                    "Bilateral",
                    "Gaussian",
                    "Median",
                    "TV",
                    "SV",
                    "HV",
                ]
            ],
            GUIMeta("Algorithm", "Denoising algorithm name."),
        ] = None
        gaussian_noise: Annotated[
            float,
            GUIMeta(
                "Gaussian noise",
                "Add artificial Gaussian noise before denoising.",
                min=0.0,
            ),
        ] = 0.0
        poisson_noise: Annotated[
            bool,
            GUIMeta("Poisson noise", "Add artificial Poisson noise before denoising."),
        ] = False
        manual_sigma: Annotated[
            float,
            GUIMeta(
                "Manual sigma",
                "Manual assumed Gaussian noise standard deviation.",
                min=0.0,
            ),
        ] = 0.0
        stabilize_poisson: Annotated[
            bool,
            GUIMeta(
                "Stabilize Poisson",
                "Use variance stabilization for Poisson noise removal.",
            ),
        ] = False
        patch: Annotated[int, GUIMeta("Patch", "Patch half-size.", min=0, step=1)] = 3
        neighborhood: Annotated[
            int, GUIMeta("Neighborhood", "Neighborhood half-size.", min=0, step=1)
        ] = 7
        denoising_parameter: Annotated[
            float,
            GUIMeta("Denoising parameter", "Algorithm-specific denoising parameter."),
        ] = -1.0
        sparsity_parameter: Annotated[
            float,
            GUIMeta(
                "Sparsity parameter", "SV/HV sparsity parameter.", min=0.1, max=0.9
            ),
        ] = 0.6
        iterations: Annotated[
            int,
            GUIMeta("Iterations", "Number of iterations for NDSafir.", min=1, step=1),
        ] = 4

    class Outputs(IOModel):
        output_image: Annotated[
            Path,
            ImageSpec(semantics={Semantic.INTENSITY}, formats={"tiff"}),
            GUIMeta("Denoised image", "Output denoised image."),
        ] = Template("{input_image.stem}_denoised.tif")

    def process_row(self, arguments: Arguments, *, context: Any = None) -> Any:
        first = _require_integer(arguments.first, "first", minimum=0)
        last = _require_integer(arguments.last, "last", minimum=-1)
        if last != -1 and last < first:
            raise ValueError("last must be -1 or greater than or equal to first.")
        alpha = _require_finite(arguments.alpha, "alpha", minimum=0.0, maximum=1.0)
        scale = _require_finite(arguments.scale, "scale", minimum=0.5, maximum=1.5)
        intensity_range = _require_finite(arguments.intensity_range, "intensity_range")
        if intensity_range != -1.0 and intensity_range <= 0:
            raise ValueError("intensity_range must be -1 or greater than zero.")
        gaussian_noise = _require_finite(
            arguments.gaussian_noise, "gaussian_noise", minimum=0.0
        )
        manual_sigma = _require_finite(
            arguments.manual_sigma, "manual_sigma", minimum=0.0
        )
        patch = _require_integer(arguments.patch, "patch", minimum=0)
        neighborhood = _require_integer(
            arguments.neighborhood, "neighborhood", minimum=0
        )
        denoising_parameter = _require_finite(
            arguments.denoising_parameter,
            "denoising_parameter",
        )
        sparsity_parameter = _require_finite(
            arguments.sparsity_parameter,
            "sparsity_parameter",
            minimum=0.1,
            maximum=0.9,
        )
        iterations = _require_integer(arguments.iterations, "iterations", minimum=1)
        algorithm = (
            None
            if arguments.algorithm is None
            else _require_choice(
                arguments.algorithm, "algorithm", _DENOISING_ALGORITHMS
            )
        )
        if not isinstance(arguments.poisson_noise, bool):
            raise ValueError("poisson_noise must be true or false.")
        if not isinstance(arguments.stabilize_poisson, bool):
            raise ValueError("stabilize_poisson must be true or false.")
        output_path = _ensure_output_parent(arguments.output_image)
        command: list[Any] = [
            "denoise",
            "-i",
            arguments.input_image,
            "-o",
            output_path,
            "-first",
            first,
            "-last",
            last,
            "-alpha",
            alpha,
            "-scale",
            scale,
            "-range",
            intensity_range,
            "-ng",
            gaussian_noise,
            "-msg",
            manual_sigma,
            "-patch",
            patch,
            "-neigh",
            neighborhood,
            "-denoisep",
            denoising_parameter,
            "-sparsep",
            sparsity_parameter,
            "-iter",
            iterations,
        ]
        if arguments.poisson_noise:
            command.append("-np")
        if arguments.stabilize_poisson:
            command.append("-stab")
        if algorithm is not None:
            command += ["-algo", algorithm]
        _run_with_staged_output(command, output_path)
        return self.Outputs(output_image=output_path)
