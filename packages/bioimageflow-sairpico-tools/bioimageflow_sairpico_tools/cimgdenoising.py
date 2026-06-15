"""SAIRPICO CImg denoising tool."""

from pathlib import Path
import sys
from typing import Annotated, Any, Literal, Optional

from bioimageflow_core import (
    Arguments,
    Category,
    GUIMeta,
    ImageSpec,
    IOModel,
    ProcessingTool,
    Semantic,
    Template,
)

package_root = str(Path(__file__).resolve().parent.parent)
if package_root not in sys.path:
    sys.path.insert(0, package_root)

from bioimageflow_sairpico_tools._common import (  # noqa: E402
    IntensityImage,
    _ensure_output_parent,
    _run,
    cimgdenoising_env,
)


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
            Optional[Literal["BM3D", "NLBayes", "NLMeans", "BayesNLmeans", "SAFIR", "PEWA", "OWF", "DCT", "Wiener", "Bilateral", "Gaussian", "Median", "TV", "SV", "HV"]],
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
