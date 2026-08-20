# SpitfireDeconvolution

## Purpose

`SpitfireDeconvolution` runs SAIRPICO SPITFIR(e) deconvolution in `2D`,
`2D Slice`, or `3D` mode. It exposes the SPITFIR(e) regularization, weighting,
method, padding, and iteration parameters used by the underlying `simglib`
commands.

## Inputs

- `input_image`: intensity image in `png`, `tif`, or `tiff` format.
- `deconvolution_type`: one of `2D`, `2D Slice`, or `3D`. Default `2D`.
- `sigma`: Gaussian PSF width for non-3D modes. Default `1.5`.
- `psf_image`: existing 3D PSF image required for `3D` mode.
- `regularization`: SPITFIR(e) regularization parameter as `pow(2, -x)`.
  Default `12.0`.
- `weighting`: weighting parameter between `0.0` and `1.0`. Default `0.6`.
- `method`: `HV` or `SV`. Default `HV`.
- `padding`: whether to use padding at borders. Default `False`.
- `niter`: iteration count. Default `200`; minimum `1`.

## Outputs

- `output_image`: deconvolved image. The default template is
  `{input_image.stem}_deconvolved.tif`.

## Assumptions

- `HV` and `SV` are passed directly to the external command.
- Non-3D modes use `sigma`; `3D` mode requires an existing PSF image.
- Inputs are scaled and sampled appropriately for the selected parameters.

## Dependencies and Core Libraries

- BioImageFlow core processing and schema classes.
- SAIRPICO `simglib` environment with `bioimageit::simglib==0.1.2`.
- External commands: `simgspitfiredeconv2d`,
  `simgspitfiredeconv2dslice`, and `simgspitfiredeconv3d`.

## Minimal Example

```python
from bioimageflow_core import Arguments
from bioimageflow_sairpico_tools import SpitfireDeconvolution

result = SpitfireDeconvolution().process_row(
    Arguments(
        input_image="input.tif",
        deconvolution_type="3D",
        sigma=1.5,
        psf_image="psf.tif",
        regularization=12.0,
        weighting=0.6,
        method="HV",
        padding=False,
        niter=150,
        output_image="spitfire.tif",
    )
)
```

## Expected Results

The tool writes `spitfire.tif`. In `3D` mode, the command includes `-psf` and
the supplied PSF path; in non-3D modes it includes `-sigma` instead.

## Failure Modes

- Missing `simgspitfiredeconv*` binary.
- The mode, method, weighting, iteration count, or another numeric parameter fails validation.
- `3D` mode is requested with a missing PSF file.
- Invalid method, regularization, or image layout causes subprocess failure.
- Output cannot be written.
