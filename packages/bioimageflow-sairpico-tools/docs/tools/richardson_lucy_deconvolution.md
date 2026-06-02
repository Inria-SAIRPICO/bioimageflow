# RichardsonLucyDeconvolution

## Purpose

`RichardsonLucyDeconvolution` runs SAIRPICO Richardson-Lucy deconvolution in
`2D`, `2D Slice`, or `3D` mode. It is appropriate for microscopy images where a
Poisson-noise deconvolution baseline is useful and the SAIRPICO binaries are
available.

## Inputs

- `input_image`: intensity image in `png`, `tif`, or `tiff` format.
- `deconvolution_type`: one of `2D`, `2D Slice`, or `3D`. Default `2D`.
- `sigma`: Gaussian PSF width for `2D` and `2D Slice` modes. Default `1.5`.
- `psf_image`: existing 3D PSF image required only for `3D` mode.
- `niter`: iteration count. Default `15`; minimum `1`.
- `regularization_lambda`: regularization value for non-3D modes. Default
  `0.0`. The wrapper passes it to the legacy `-lambda` CLI option.
- `padding`: whether to process border pixels with padding. Default `False`.

## Outputs

- `output_image`: deconvolved intensity image. The default template is
  `{input_image.stem}_deconvolved{ext}`.

## Assumptions

- Non-3D modes use the scalar `sigma` parameter rather than an explicit PSF.
- `3D` mode requires `psf_image` to exist before the subprocess is started.
- The input image layout matches the selected SAIRPICO mode.

## Dependencies and Core Libraries

- BioImageFlow core processing and templating.
- SAIRPICO `simglib` environment with `sylvainprigent::simglib=0.1.2`.
- External commands: `simgrichardsonlucy2d`,
  `simgrichardsonlucy2dslice`, and `simgrichardsonlucy3d`.

## Minimal Example

```python
from bioimageflow_core import Arguments
from bioimageflow_sairpico_tools import RichardsonLucyDeconvolution

result = RichardsonLucyDeconvolution().process_row(
    Arguments(
        input_image="denoised.tif",
        deconvolution_type="2D",
        sigma=1.2,
        psf_image=None,
        niter=3,
        regularization_lambda=0.01,
        padding=True,
        output_image="richardson_lucy.tif",
    )
)
```

## Expected Results

The tool writes `richardson_lucy.tif` and returns that path. In the example, the
wrapper invokes `simgrichardsonlucy2d` with `-sigma 1.2`, `-niter 3`,
`-lambda 0.01`, and `-padding true`.

## Failure Modes

- Selected SAIRPICO command is not installed.
- `3D` mode is requested without an existing `psf_image`.
- The command exits with a non-zero status because of unsupported input shape,
  format, or parameter values.
- Output path creation or writing fails.
