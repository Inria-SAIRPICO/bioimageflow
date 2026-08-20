# WienerDeconvolution

## Purpose

`WienerDeconvolution` runs SAIRPICO Wiener deconvolution in `2D`, `2D Slice`,
or `3D` mode. Use it as a regularized linear deconvolution baseline when a
sigma-based 2D approximation or a measured/synthetic 3D PSF is available.

## Inputs

- `input_image`: intensity image in `png`, `tif`, or `tiff` format.
- `deconvolution_type`: one of `2D`, `2D Slice`, or `3D`. Default `2D`.
- `sigma`: Gaussian PSF width for non-3D modes. Default `1.5`.
- `psf_image`: existing volumetric PSF image required for `3D` mode.
- `regularization_lambda`: regularization value passed as CLI `-lambda`.
  Default `0.01`.
- `padding`: whether to process border pixels with padding. Default `False`.

## Outputs

- `output_image`: deconvolved image. The default template is
  `{input_image.stem}_deconvolved.tif`.

## Assumptions

- The chosen mode matches the input image dimensionality.
- `3D` mode uses `psf_image`; non-3D modes use `sigma`.
- The regularization scale is appropriate for the image intensity range.

## Dependencies and Core Libraries

- BioImageFlow core processing classes.
- SAIRPICO `simglib` environment with `bioimageit::simglib==0.1.2`.
- External commands: `simgwiener2d`, `simgwiener2dslice`, and `simgwiener3d`.

## Minimal Example

```python
from bioimageflow_core import Arguments
from bioimageflow_sairpico_tools import WienerDeconvolution

result = WienerDeconvolution().process_row(
    Arguments(
        input_image="input.tif",
        deconvolution_type="2D",
        sigma=1.5,
        psf_image=None,
        regularization_lambda=0.02,
        padding=False,
        output_image="wiener.tif",
    )
)
```

## Expected Results

The tool writes the deconvolved image and returns its path. For the example, the
command starts with `simgwiener2d` and includes `-sigma 1.5` and
`-lambda 0.02`.

## Failure Modes

- Missing SAIRPICO command or incompatible runtime platform.
- The mode or a numeric or boolean parameter fails validation.
- Missing PSF file for `3D` mode.
- Non-zero subprocess exit due to unsupported image data or parameters.
- Output path cannot be created or written.
