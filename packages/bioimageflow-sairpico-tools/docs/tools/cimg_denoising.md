# CImgDenoising

## Purpose

`CImgDenoising` wraps the SAIRPICO CImg `denoise` command for 2D+T denoising
with patch, neighborhood, noise-model, and algorithm controls. It is useful for
testing CImg denoising baselines without moving those command-line options into
workflow code.

## Inputs

- `input_image`: intensity image in `png`, `tif`, or `tiff` format.
- `first`: first image index; `0` uses the command default. Default `0`.
- `last`: last image index; `-1` uses the full depth or time range. Default
  `-1`.
- `alpha`: input/output alpha mixing. Default `0.0`.
- `scale`: volume resize factor. Default `1.0`.
- `intensity_range`: automatic intensity scaling with `-1` or manual scaling.
  Default `1.0`.
- `algorithm`: optional algorithm name such as `BM3D`, `NLBayes`, `PEWA`,
  `Wiener`, `Bilateral`, `Median`, `TV`, `SV`, or `HV`. Default `None`.
- `gaussian_noise`: artificial Gaussian noise level before denoising. Default
  `0.0`.
- `poisson_noise`: add artificial Poisson noise before denoising. Default
  `False`.
- `manual_sigma`: manual assumed Gaussian noise standard deviation. Default
  `0.0`.
- `stabilize_poisson`: use variance stabilization for Poisson noise removal.
  Default `False`.
- `patch`: patch half-size. Default `3`.
- `neighborhood`: neighborhood half-size. Default `7`.
- `denoising_parameter`: algorithm-specific denoising parameter. Default
  `-1.0`.
- `sparsity_parameter`: SV/HV sparsity parameter. Default `0.6`.
- `iterations`: iteration count for NDSafir. Default `4`.

## Outputs

- `output_image`: denoised intensity image. The default template is
  `{input_image.stem}_denoised{ext}`.

## Assumptions

- The selected algorithm name is supported by the installed `denoise` command.
- Boolean options are omitted unless enabled.
- Parameter meaning is delegated to the CImg backend.

## Dependencies and Core Libraries

- BioImageFlow core processing abstractions.
- SAIRPICO `cimgdenoising` environment with
  `bioimageit::cimgdenoising==1.0.0`.
- External command: `denoise`.

## Minimal Example

```python
from bioimageflow_core import Arguments
from bioimageflow_sairpico_tools import CImgDenoising

result = CImgDenoising().process_row(
    Arguments(
        input_image="input.tif",
        first=0,
        last=-1,
        alpha=0.0,
        scale=1.0,
        intensity_range=1.0,
        algorithm="PEWA",
        gaussian_noise=0.0,
        poisson_noise=False,
        manual_sigma=0.0,
        stabilize_poisson=False,
        patch=3,
        neighborhood=7,
        denoising_parameter=-1.0,
        sparsity_parameter=0.6,
        iterations=4,
        output_image="denoised.tif",
    )
)
```

## Expected Results

The tool writes `denoised.tif`. When `algorithm` is set, the command ends with
`-algo` and the selected algorithm name; when `poisson_noise` or
`stabilize_poisson` are false, their flags are omitted.

## Failure Modes

- The `denoise` executable or its runtime libraries are missing.
- The selected algorithm is unsupported by the installed binary.
- Parameter combinations are invalid for the input image.
- The subprocess exits non-zero or cannot write the output.
