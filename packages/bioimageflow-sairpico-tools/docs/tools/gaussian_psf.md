# GaussianPSF

## Purpose

`GaussianPSF` generates a 3D Gaussian point-spread-function image by calling the
SAIRPICO `simggaussian3dpsf` command. Use it when a synthetic PSF is sufficient
for 3D restoration experiments or for building a reproducible deconvolution
input without microscope-specific PSF measurement.

## Inputs

- `width`: output image width in pixels. Default `256`; minimum `1`.
- `height`: output image height in pixels. Default `256`; minimum `1`.
- `depth`: number of Z planes. Default `20`; minimum `1`.
- `sigmaxy`: Gaussian sigma in XY pixels. Default `1.0`; non-negative.
- `sigmaz`: Gaussian sigma in Z planes. Default `1.0`; non-negative.

## Outputs

- `output_image`: generated 3D intensity TIFF. The default template is
  `gaussian_psf.tif`.

## Assumptions

- The requested dimensions fit available memory and the output format expected
  by downstream deconvolution tools.
- `sigmaxy` and `sigmaz` describe a useful synthetic PSF for the image scale.
- The tool does not validate optical realism; it passes parameters to the
  underlying SAIRPICO binary.

## Dependencies and Core Libraries

- BioImageFlow core `ProcessingTool`, `ImageSpec`, and templated outputs.
- SAIRPICO `simglib` environment with `bioimageit::simglib=0.1.2`.
- External command: `simggaussian3dpsf`.

## Minimal Example

```python
from bioimageflow_core import Arguments
from bioimageflow_sairpico_tools import GaussianPSF

result = GaussianPSF().process_row(
    Arguments(
        width=32,
        height=24,
        depth=8,
        sigmaxy=1.2,
        sigmaz=2.3,
        output_image="psf.tif",
    )
)
print(result.output_image)
```

## Expected Results

The tool writes `psf.tif` and returns its path as `output_image`. The command
line contains `simggaussian3dpsf`, the output path, sigma values, and requested
dimensions.

## Failure Modes

- `simggaussian3dpsf` is not installed or not on `PATH`.
- The command exits with a non-zero status.
- The output directory cannot be created or written.
- Very large dimensions exhaust memory or disk space in the external process.
